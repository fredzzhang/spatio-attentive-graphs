"""
Utilities

Fred Zhang <frederic.zhang@anu.edu.au>

The Australian National University
Australian Centre for Robotic Vision
"""

import os
import json
import time
import torch
import pickle
import numpy as np
from tqdm import tqdm
import torch.distributed as dist

from torch.utils.data import Dataset
from torchvision.ops.boxes import box_iou

import pocket
from pocket.core import DistributedLearningEngine
from pocket.utils import DetectionAPMeter, HandyTimer, BoxPairAssociation, all_gather

def custom_collate(batch):
    images = []
    detections = []
    targets = []
    for im, det, tar in batch:
        images += im
        detections += det
        targets += tar
    return images, detections, targets

class CustomisedDataset(Dataset):
    def __init__(self, dataset, detection_dir,
            # Parameters for preprocessing
            human_idx,
            box_score_thresh_h=0.3,
            box_score_thresh_o=0.3
            ):

        self.dataset = dataset
        self.detection_dir = detection_dir

        self.human_idx = human_idx
        self.box_score_thresh_h = box_score_thresh_h
        self.box_score_thresh_o = box_score_thresh_o

    def __len__(self):
        return len(self.dataset)

    def filter_detections(self, detection):
        """Perform NMS and remove low scoring examples"""

        boxes = torch.as_tensor(detection['boxes'])
        labels = torch.as_tensor(detection['labels'])
        scores = torch.as_tensor(detection['scores'])

        # Filter out low scoring human boxes
        idx = torch.nonzero(labels == self.human_idx).squeeze(1)
        keep_idx = idx[torch.nonzero(scores[idx] >= self.box_score_thresh_h).squeeze(1)]

        # Filter out low scoring object boxes
        idx = torch.nonzero(labels != self.human_idx).squeeze(1)
        keep_idx = torch.cat([
            keep_idx,
            idx[torch.nonzero(scores[idx] >= self.box_score_thresh_o).squeeze(1)]
        ])

        boxes = boxes[keep_idx].view(-1, 4)
        scores = scores[keep_idx].view(-1)
        labels = labels[keep_idx].view(-1)

        return dict(boxes=boxes, labels=labels, scores=scores)

    def __getitem__(self, i):
        image, target = self.dataset[i]
        target['labels'] = target['verb']

        # Convert ground truth boxes to zero-based index and the
        # representation from pixel indices to coordinates
        target['boxes_h'][:, :2] -= 1
        target['boxes_o'][:, :2] -= 1

        detection_path = os.path.join(
            self.detection_dir,
            self.dataset.filename(i).replace('jpg', 'json')
        )
        with open(detection_path, 'r') as f:
            detection = pocket.ops.to_tensor(json.load(f),
                input_format='dict')

        detection = self.filter_detections(detection)

        return [image], [detection], [target]


def test(net, test_loader):
    testset = test_loader.dataset.dataset
    associate = BoxPairAssociation(min_iou=0.5)

    ap_test = DetectionAPMeter(
        600, nproc=1,
        num_gt=testset.anno_interaction,
        algorithm='11P'
    )
    net.eval()
    for batch in tqdm(test_loader):
        inputs = pocket.ops.relocate_to_cuda(batch[:-1])
        with torch.no_grad():
            output = net(*inputs)
        if output is None:
            continue

        # Batch size is fixed as 1 for inference
        assert len(output) == 1, "Batch size is not 1"
        output = pocket.ops.relocate_to_cpu(output[0])
        target = batch[-1][0]
        # Format detections
        box_idx = output['index']
        boxes_h = output['boxes_h'][box_idx]
        boxes_o = output['boxes_o'][box_idx]
        objects = output['object'][box_idx]
        scores = output['scores']
        verbs = output['prediction']
        interactions = torch.tensor([
            testset.object_n_verb_to_interaction[o][v]
            for o, v in zip(objects, verbs)
        ])
        # Associate detected pairs with ground truth pairs
        labels = torch.zeros_like(scores)
        unique_hoi = interactions.unique()
        for hoi_idx in unique_hoi:
            gt_idx = torch.nonzero(target['hoi'] == hoi_idx).squeeze(1)
            det_idx = torch.nonzero(interactions == hoi_idx).squeeze(1)
            if len(gt_idx):
                labels[det_idx] = associate(
                    (target['boxes_h'][gt_idx].view(-1, 4),
                    target['boxes_o'][gt_idx].view(-1, 4)),
                    (boxes_h[det_idx].view(-1, 4),
                    boxes_o[det_idx].view(-1, 4)),
                    scores[det_idx].view(-1)
                )

        ap_test.append(scores, interactions, labels)

    return ap_test.eval()

class CustomisedEngine(DistributedLearningEngine):
    def __init__(self, net, train_loader, **kwargs):
        super().__init__(net, None, train_loader, **kwargs)

    def _on_each_iteration(self):
        self._state.optimizer.zero_grad()
        self._state.output = self._state.net(
            *self._state.inputs, targets=self._state.targets)
        self._state.loss = self._state.output.pop()
        self._state.loss.backward()
        self._state.optimizer.step()

    def _on_end_epoch(self):
        super()._on_end_epoch()
        # Print time
        if self._rank == 0:
            print("Epoch: {} finished at (+{:.2f}s)".format(
                    self._state.epoch, time.time() - self._dawn
                ))

    # def _synchronise_and_log_results(self, output, meter):
    #     scores = []; pred = []; labels = []
    #     # Collate results within the batch
    #     for result in output:
    #         scores.append(torch.cat(result["scores"]).detach().cpu().numpy())
    #         pred.append(torch.cat(result["labels"]).cpu().float().numpy())
    #         labels.append(torch.cat(result["gt_labels"]).cpu().numpy())
    #     # Sync across subprocesses
    #     all_results = np.stack([
    #         np.concatenate(scores),
    #         np.concatenate(pred),
    #         np.concatenate(labels)
    #     ])
    #     all_results_sync = all_gather(all_results)
    #     # Collate and log results in master process
    #     if self._rank == 0:
    #         scores, pred, labels = torch.from_numpy(
    #             np.concatenate(all_results_sync, axis=1)
    #         ).unbind(0)
    #         meter.append(scores, pred, labels)

    # def test(self, min_iou=0.5):
    #     """Test the network and return classification mAP"""
    #     # Instantiate the AP meter in the master process
    #     if self._rank == 0:
    #         ap_test = DetectionAPMeter(self.num_classes, algorithm="INT")
        
    #     self._state.net.eval()
    #     for batch in self.test_loader:
    #         inputs = pocket.ops.relocate_to_cuda(batch)
    #         with torch.no_grad():
    #             results = self._state.net(*inputs)
    #         if results is None:
    #             continue

    #         self._synchronise_and_log_results(results, ap_test)

    #     # Evaluate mAP in master process
    #     if self._rank == 0:
    #         return ap_test.eval()
    #     else:
    #         return None
