import os
import time
import torch
import pickle
import argparse
import torchvision
from torch import nn
from tqdm import tqdm
from torch.utils.data import DataLoader, Dataset
from torchvision.ops.boxes import box_iou

import pocket
from pocket.data import HICODet
from pocket.utils import NumericalMeter, DetectionAPMeter, HandyTimer

from utils import preprocessed_collate, PreprocessedDataset

class LabelDataset(Dataset):
    def __init__(self, src_path):
        with open(src_path, 'rb') as f:
            data = pickle.load(f)
        self.prior = data['prior']
        self.labels = data['labels']
    def __len__(self):
        return len(self.labels)
    def __getitem__(self, idx):
        return self.prior[idx], self.labels[idx]

class Model(nn.Module):
    def __init__(self, input_size=117, representation_size=1024, num_classes=117):
        super().__init__()
        self.head = nn.Sequential(
            nn.Linear(input_size, representation_size),
            nn.ReLU(),
            nn.Linear(representation_size, representation_size),
            nn.ReLU(),
            nn.Linear(representation_size, num_classes)
        )
    def forward(self, prior, labels):
        output = self.head(labels)
        i, j = torch.nonzero(prior).unbind(1)
        return output[i, j], labels[i, j], j

@torch.no_grad()
def test(net, test_loader):
    net.eval()
    ap_test = DetectionAPMeter(117, algorithm='11P')
    for batch in tqdm(test_loader):
        batch_cuda = pocket.ops.relocate_to_cuda(batch)
        logits, labels, pred = net(*batch_cuda)
        # Collate results within the batch
        ap_test.append(
            torch.sigmoid(logits),
            pred, labels
        )
    return ap_test.eval()

def main(args):

    torch.cuda.set_device(0)
    torch.backends.cudnn.benchmark = False

    trainset = LabelDataset('./preprocessed/labels_train2015.pkl')
    testset = PreprocessedDataset('./preprocessed/labels_test2015.pkl')

    train_loader = DataLoader(
        dataset=trainset,
        batch_size=args.batch_size,
        num_workers=args.num_workers, pin_memory=True, shuffle=True
    )

    test_loader = DataLoader(
        dataset=testset,
        batch_size=args.batch_size,
        num_workers=args.num_workers, pin_memory=True
    )


    # Fix random seed for model synchronisation
    torch.manual_seed(args.random_seed)

    net = Model()

    if os.path.exists(args.model_path):
        print("Loading model from ", args.model_path)
        net.load_state_dict(torch.load(args.model_path)['model_state_dict'])
    if not os.path.exists(args.cache_dir):
        os.makedirs(args.cache_dir)

    net.cuda()

    net_params = [p for p in net.parameters() if p.requires_grad]
    optimizer = torch.optim.SGD(net_params,
        lr=args.learning_rate,
        momentum=args.momentum,
        weight_decay=args.weight_decay
    )

    lr_scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer,
        milestones=args.milestones,
        gamma=args.lr_decay
    )

    running_loss = NumericalMeter(maxlen=args.print_interval)
    t_data = NumericalMeter(maxlen=args.print_interval)
    t_iteration = NumericalMeter(maxlen=args.print_interval)
    timer = HandyTimer(2)

    iterations = 0

    for epoch in range(args.num_epochs):
        #################
        # on_start_epoch
        #################
        net.train()
        ap_train = DetectionAPMeter(117, algorithm='11P')
        timestamp = time.time()
        for batch in train_loader:
            ####################
            # on_start_iteration
            ####################
            iterations += 1
            batch_cuda = pocket.ops.relocate_to_cuda(batch)
            t_data.append(time.time() - timestamp)
            ####################
            # on_each_iteration
            ####################
            optimizer.zero_grad()
            logits, labels, pred = net(*batch_cuda)
            loss = nn.functional.binary_cross_entropy_with_logits(logits, labels)
            loss.backward()
            optimizer.step()

            # Collate results within the batch
            ap_train.append(
                torch.sigmoid(logits),
                pred, labels
            )
            ####################
            # on_end_iteration
            ####################
            running_loss.append(loss.item())
            t_iteration.append(time.time() - timestamp)
            timestamp = time.time()
            if iterations % args.print_interval == 0:
                avg_loss = running_loss.mean()
                sum_t_data = t_data.sum()
                sum_t_iter = t_iteration.sum()
                
                num_iter = len(train_loader)
                n_d = len(str(num_iter))
                print(
                    "Epoch [{}/{}], Iter. [{}/{}], "
                    "Loss: {:.4f}, "
                    "Time[Data/Iter.]: [{:.2f}s/{:.2f}s]".format(
                    epoch+1, args.num_epochs,
                    str(iterations - num_iter * epoch).zfill(n_d),
                    num_iter, avg_loss, sum_t_data, sum_t_iter
                ))
                running_loss.reset()
                t_data.reset(); t_iteration.reset()
        #################
        # on_end_epoch
        #################
        lr_scheduler.step()
        torch.save({
            'iteration': iterations,
            'epoch': epoch+1,
            'model_state_dict': net.state_dict(),
            'optim_state_dict': optimizer.state_dict()
            }, os.path.join(args.cache_dir, 'ckpt_{:05d}_{:02d}.pt'.\
                    format(iterations, epoch+1)))
        
        with timer:
            ap_1 = ap_train.eval()
        with timer:
            ap_2 = test(net, test_loader)
        print("Epoch: {} | training mAP: {:.4f}, eval. time: {:.2f}s |"
            "test mAP: {:.4f}, total time: {:.2f}s".format(
                epoch+1, ap_1.mean().item(), timer[0],
                ap_2.mean().item(), timer[1]
        ))

if __name__ == '__main__':
    
    parser = argparse.ArgumentParser(description="Train an interaction head")
    parser.add_argument('--num-epochs', default=20, type=int)
    parser.add_argument('--random-seed', default=1, type=int)
    parser.add_argument('--learning-rate', default=0.001, type=float)
    parser.add_argument('--momentum', default=0.9, type=float)
    parser.add_argument('--weight-decay', default=1e-4, type=float)
    parser.add_argument('--batch-size', default=1024, type=int,
                        help="Batch size for each subprocess")
    parser.add_argument('--lr-decay', default=0.1, type=float,
                        help="The multiplier by which the learning rate is reduced")
    parser.add_argument('--milestones', nargs='+', default=[10, 15], type=int,
                        help="The epoch number when learning rate is reduced")
    parser.add_argument('--num-workers', default=2, type=int)
    parser.add_argument('--print-interval', default=100, type=int)
    parser.add_argument('--model-path', default='', type=str)
    parser.add_argument('--cache-dir', type=str, default='./checkpoints')

    args = parser.parse_args()
    print(args)

    main(args)