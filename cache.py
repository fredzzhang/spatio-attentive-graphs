"""
Run inference and cache detections as .mat files

Fred Zhang <frederic.zhang@anu.edu.au>

The Australian National University
Australian Centre for Robotic Vision
"""

import os
import json
import torch
import argparse
import torchvision
import numpy as np
import scipy.io as sio

from tqdm import tqdm
from torch.utils.data import DataLoader

import pocket
from pocket.data import HICODet

from models import InteractGraphNet
from utils import CustomisedDataset, custom_collate

def inference(net, dataloader, coco2hico, cache_dir):
    dataset = dataloader.dataset.dataset
    net.eval()
    all_results = np.empty((600, 9658), dtype=object)

    object2int = dataset.object_to_interaction
    for i, batch in enumerate(tqdm(dataloader)):
        inputs = pocket.ops.relocate_to_cuda(batch[:-1])
        with torch.no_grad():
            output = net(*inputs)
        if output is None:
            continue

        # Batch size is fixed as 1 for inference
        assert len(output) == 1, "Batch size is not 1"
        output = pocket.ops.relocate_to_cpu(output[0])
        # NOTE Index i is the intra-index amongst images excluding those without
        # ground truth box pairs
        image_idx = dataset._idx[i]

        box_idx = output['index']
        boxes_h = output['boxes_h'][box_idx]
        boxes_o = output['boxes_o'][box_idx]
        objects = output['object'][box_idx]

        scores = output['scores']
        verbs = output['prediction']
        interactions = torch.tensor([
            dataset.object_n_verb_to_interaction[o][v]
            for o, v in zip(objects, verbs)
        ])

        # Group box pairs with the same predicted class
        permutation = interactions.argsort()
        boxes_h = boxes_h[permutation]
        boxes_o = boxes_o[permutation]
        interactions = interactions[permutation]
        scores = scores[permutation]

        # Store results
        unique_class, counts = interactions.unique(return_counts=True)
        n = 0
        for cls_id, cls_num in zip(unique_class, counts):
            all_results[cls_id.long(), image_idx] = torch.cat([
                boxes_h[n: n + cls_num],
                boxes_o[n: n + cls_num],
                scores[n: n + cls_num, None]
            ], dim=1).numpy()
            n += cls_num

    # Replace None with size (0,0) arrays
    for i in range(600):
        for j in range(9658):
            if all_results[i, j] is None:
                all_results[i, j] = np.zeros((0, 0))
    # Cache results
    for object_idx in coco2hico:
        interaction_idx = object2int[coco2hico[object_idx]]
        sio.savemat(
            os.path.join(cache_dir, 'detections_{}.mat'.format(object_idx.zfill(2))),
            dict(all_boxes=all_results[interaction_idx])
        )

def main(args):
    torch.cuda.set_device(0)
    torch.backends.cudnn.benchmark = False

    if not os.path.exists(args.cache_dir):
        os.makedirs(args.cache_dir)

    with open(os.path.join(args.data_root, 'coco80tohico80.json'), 'r') as f:
        coco2hico = json.load(f)

    dataset = HICODet(
        root=os.path.join(args.data_root,
            "hico_20160224_det/images/{}".format(args.partition)),
        anno_file=os.path.join(args.data_root,
            "instances_{}.json".format(args.partition)),
        transform=torchvision.transforms.ToTensor(),
        target_transform=pocket.ops.ToTensor(input_format='dict')
    )    
    dataloader = DataLoader(
        dataset=CustomisedDataset(dataset,
            os.path.join(args.data_root,
            "fasterrcnn_resnet50_fpn_detections/{}".format(args.partition)),
            human_idx=49,
            box_score_thresh_h=args.human_thresh,
            box_score_thresh_o=args.object_thresh
        ), collate_fn=custom_collate, batch_size=1,
        num_workers=args.num_workers, pin_memory=True
    )

    net = InteractGraphNet(
        dataset.object_to_verb, 49,
        num_iterations=args.num_iter
    )
    if os.path.exists(args.model_path):
        print("Loading model from ", args.model_path)
        checkpoint = torch.load(args.model_path, map_location="cpu")
        net.load_state_dict(checkpoint['model_state_dict'])

    net.cuda()
    
    inference(net, dataloader, coco2hico, args.cache_dir)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train an interaction head")
    parser.add_argument('--data-root', required=True, type=str)
    parser.add_argument('--cache-dir', default='matcache', type=str)
    parser.add_argument('--partition', default='test2015', type=str)
    parser.add_argument('--num-iter', default=3, type=int,
                        help="Number of iterations to run message passing")
    parser.add_argument('--human-thresh', default=0.5, type=float)
    parser.add_argument('--object-thresh', default=0.5, type=float)
    parser.add_argument('--num-workers', default=2, type=int)
    parser.add_argument('--model-path', default='', type=str)
    
    args = parser.parse_args()
    print(args)

    main(args)
