"""
Test a model and compute detection mAP

Fred Zhang <frederic.zhang@anu.edu.au>

The Australian National University
Australian Centre for Robotic Vision
"""

import os
import torch
import argparse
import torchvision
from torch.utils.data import DataLoader

import pocket
from pocket.data import HICODet

from models import SpatioAttentiveGraph
from utils import CustomisedDataset, custom_collate, test

def main(args):
    torch.cuda.set_device(0)
    torch.backends.cudnn.benchmark = False

    num_anno = torch.tensor(HICODet(None, anno_file=os.path.join(
        args.data_root, 'instances_train2015.json')).anno_interaction)
    rare = torch.nonzero(num_anno < 10).squeeze(1)
    non_rare = torch.nonzero(num_anno >= 10).squeeze(1)

    dataset = HICODet(
        root=os.path.join(args.data_root,
            "hico_20160224_det/images/{}".format(args.partition)),
        anno_file=os.path.join(args.data_root,
            "instances_{}.json".format(args.partition)),
        target_transform=pocket.ops.ToTensor(input_format='dict')
    )
    detection_path = os.path.join(
        args.data_root,
        "detections/{}".format(args.partition)
    )
    dataloader = DataLoader(
        dataset=CustomisedDataset(dataset,
            args.detection_dir,
            human_idx=49,
            box_score_thresh_h=args.human_thresh,
            box_score_thresh_o=args.object_thresh
        ), collate_fn=custom_collate, batch_size=1,
        num_workers=args.num_workers, pin_memory=True
    )

    net = SpatioAttentiveGraph(
        dataset.object_to_verb, 49,
        num_iterations=args.num_iter
    )
    epoch = 0
    if os.path.exists(args.model_path):
        print("Loading model from ", args.model_path)
        checkpoint = torch.load(args.model_path, map_location="cpu")
        net.load_state_dict(checkpoint['model_state_dict'])
        epoch = checkpoint["epoch"]

    net.cuda()
    timer = pocket.utils.HandyTimer(maxlen=1)
    
    with timer:
        test_ap = test(net, dataloader)
    print("Model at epoch: {} | time elapsed: {:.2f}s\n"
        "Full: {:.4f}, rare: {:.4f}, non-rare: {:.4f}".format(
        epoch, timer[0], test_ap.mean(),
        test_ap[rare].mean(), test_ap[non_rare].mean()
    ))

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train an interaction head")
    parser.add_argument('--data-root', default='hicodet', type=str)
    parser.add_argument('--detection-dir', default='hicodet/detections/test2015',
                        type=str, help="Directory where detection files are stored")
    parser.add_argument('--partition', default='test2015', type=str)
    parser.add_argument('--num-iter', default=2, type=int,
                        help="Number of iterations to run message passing")
    parser.add_argument('--human-thresh', default=0.2, type=float)
    parser.add_argument('--object-thresh', default=0.2, type=float)
    parser.add_argument('--num-workers', default=2, type=int)
    parser.add_argument('--model-path', default='', type=str)
    
    args = parser.parse_args()
    print(args)

    main(args)
