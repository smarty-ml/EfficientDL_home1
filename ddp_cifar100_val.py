import os

import torch
import torch.distributed as dist
import torch.nn as nn
import torch.nn.functional as F
import torchvision.transforms as transforms
from torch.utils.data import DataLoader, TensorDataset
from torch.utils.data.distributed import DistributedSampler
from torchvision.datasets import CIFAR100

from syncbn import SyncBatchNorm

torch.set_num_threads(1)


def convert_dataset_to_tensor(dataset):
    X, y = [], []
    for i in range(len(dataset)):
        X_i, y_i = dataset[i]
        X.append(torch.cat((X_i, torch.ones(size=X_i.size()[1:]).unsqueeze(0) * y_i)))
    return torch.stack(X)


def init_process(local_rank, fn, backend="nccl"):
    """Initialize the distributed environment."""
    dist.init_process_group(backend, rank=local_rank)
    size = dist.get_world_size()
    fn(local_rank, size)


class Net(nn.Module):
    """
    A very simple model with minimal changes from the tutorial, used for the sake of simplicity.
    Feel free to replace it with EffNetV2-XL once you get comfortable injecting SyncBN into models programmatically.
    """

    def __init__(self):
        super().__init__()
        self.conv1 = nn.Conv2d(3, 32, 3, 1)
        self.conv2 = nn.Conv2d(32, 32, 3, 1)
        self.dropout1 = nn.Dropout(0.25)
        self.dropout2 = nn.Dropout(0.5)
        self.fc1 = nn.Linear(6272, 128)
        self.fc2 = nn.Linear(128, 100)
        # self.bn1 = nn.BatchNorm1d(128, affine=False)  # to be replaced with SyncBatchNorm
        self.bn1 = SyncBatchNorm(128)  # to be replaced with SyncBatchNorm

    def forward(self, x):
        x = self.conv1(x)
        x = F.relu(x)

        x = self.conv2(x)
        x = F.relu(x)

        x = F.max_pool2d(x, 2)
        x = self.dropout1(x)

        x = torch.flatten(x, 1)
        x = self.fc1(x)
        x = self.bn1(x)
        x = F.relu(x)
        x = self.dropout2(x)
        output = self.fc2(x)
        return output


def average_gradients(model):
    size = float(dist.get_world_size())
    for param in model.parameters():
        dist.all_reduce(param.grad.data, op=dist.ReduceOp.SUM)
        param.grad.data /= size


def run_training(rank, size):
    torch.manual_seed(rank)

    dataset = CIFAR100(
        "./cifar",
        transform=transforms.Compose(
            [
                transforms.ToTensor(),
                transforms.Normalize((0.5071, 0.4867, 0.4408), (0.2675, 0.2565, 0.2761)),
            ]
        ),
        download=True,
    )
    train_dataset, val_dataset = torch.utils.data.random_split(dataset, [len(dataset) - 10000, 10000])
    # where's the validation dataset?
    train_loader = DataLoader(train_dataset, sampler=DistributedSampler(train_dataset, size, rank), batch_size=64)
    process_count = dist.get_world_size()
    if rank == 0:
        val_tensor = convert_dataset_to_tensor(val_dataset)
        val_tensor_list = list(torch.split(val_tensor, int(10000 / process_count)))
        val = torch.zeros(size=val_tensor_list[0].size())
        dist.scatter(val, scatter_list=val_tensor_list)
    else:
        val = torch.zeros(size=(int(10000 / process_count), 4, 32, 32))
        dist.scatter(val, scatter_list=None)
    val_X = val[:, :3, :, :]
    val_y = val[:, 3, 0, 0]
    val_dataset = TensorDataset(val_X, val_y.type(torch.LongTensor))
    val_loader = DataLoader(val_dataset, batch_size=64)

    model = Net()
    device = torch.device("cpu")  # replace with "cuda" afterwards
    model.to(device)
    optimizer = torch.optim.SGD(model.parameters(), lr=0.01, momentum=0.5)

    num_batches = len(train_loader)

    for epoch in range(10):
        model.train()
        for data, target in train_loader:
            data = data.to(device)
            target = target.to(device)

            optimizer.zero_grad()
            output = model(data)
            loss = torch.nn.functional.cross_entropy(output, target)
            loss.backward()
            average_gradients(model)
            optimizer.step()
        # where's the validation loop?
        model.eval()
        val_acc = 0
        val_loss = 0
        val_batches_num = 0
        for data, target in val_loader:
            val_batches_num += len(target)
            data = data.to(device)
            target = target.to(device)
            output = model(data)
            loss = torch.nn.functional.cross_entropy(output, target)
            val_loss += loss.detach()
            val_acc += (output.argmax(dim=1) == target).float().sum()

        val_acc /= val_batches_num
        val_loss /= val_batches_num

        for_sync = torch.tensor([val_loss, val_acc])
        dist.all_reduce(for_sync, op=dist.ReduceOp.SUM)
        val_loss, val_acc = for_sync
        val_acc /= dist.get_world_size()
        val_loss /= dist.get_world_size()
        if rank == 0:
            print(f"Epoch {epoch}, loss: {val_loss}, acc: {val_acc}")


if __name__ == "__main__":
    local_rank = int(os.environ["LOCAL_RANK"])
    init_process(local_rank, fn=run_training, backend="gloo")  # replace with "nccl" when testing on GPUs
