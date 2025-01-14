#!/usr/bin/env python
# coding: utf-8

# In[ ]:


# set cuda visible devices
def is_notebook() -> bool:
    try:
        shell = get_ipython().__class__.__name__
        if shell == 'ZMQInteractiveShell':
            return True   # Jupyter notebook or qtconsole
        elif shell == 'TerminalInteractiveShell':
            return False  # Terminal running IPython
        else:
            return False  # Other type (?)
    except NameError:
        return False      # Probably standard Python interpreter

import os
if is_notebook():
    os.environ["CUDA_VISIBLE_DEVICES"] = "7" #"1"
    # os.environ['CUDA_LAUNCH_BLOCKING']="1"
    # os.environ['TORCH_USE_CUDA_DSA'] = "1"

import matplotlib 
if not is_notebook():
    matplotlib.use('Agg')


# In[ ]:


import os
import math
import json
import random as rnd
from typing import Optional, Callable
from tqdm import tqdm
from collections import defaultdict
from functools import partial
from datetime import datetime
from dataclasses import dataclass 

from omegaconf import OmegaConf
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset, random_split, TensorDataset
import matplotlib.pyplot as plt
import pandas as  pd
import torchvision.utils as vision_utils
from PIL import Image
import torchvision
from torchvision import transforms
from matplotlib.ticker import NullFormatter
from sklearn.decomposition import PCA

from losses.divdis import DivDisLoss 
from losses.divdis import DivDisLoss
from losses.ace import ACELoss
from losses.conf import ConfLoss
from losses.dbat import DBatLoss
from losses.pass_through import PassThroughLoss
from losses.smooth_top_loss import SmoothTopLoss
from losses.loss_types import LossType

from models.backbone import MultiHeadBackbone
from models.multi_model import MultiNetModel, freeze_heads
from models.lenet import LeNet

from spurious_datasets.cifar_mnist import get_cifar_mnist_datasets
from spurious_datasets.fmnist_mnist import get_fmnist_mnist_datasets
from spurious_datasets.toy_grid import get_toy_grid_datasets
from spurious_datasets.waterbirds import get_waterbirds_datasets
from spurious_datasets.cub import get_cub_datasets
from spurious_datasets.camelyon import get_camelyon_datasets
from spurious_datasets.multi_nli import get_multi_nli_datasets
from spurious_datasets.civil_comments import get_civil_comments_datasets
from spurious_datasets.celebA import get_celebA_datasets

from utils.utils import to_device, batch_size
from utils.act_utils import get_acts_and_labels, plot_activations, transform_activations


# # Setup Experiment

# In[ ]:


@dataclass
class Config():
    seed: int = 1
    dataset: str = "waterbirds"
    loss_type: LossType = LossType.TOPK
    batch_size: int = 32
    target_batch_size: int = 64
    epochs: int = 5
    heads: int = 2
    binary: bool = False
    model: str = "Resnet50"
    shared_backbone: bool = True
    source_weight: float = 1.0
    aux_weight: float = 1.0
    use_group_labels: bool = False
    source_cc: bool = True
    source_val_split: float = 0.2
    target_val_split: float = 0.2
    source_mix_rate: Optional[float] = 0.0
    source_01_mix_rate: Optional[float] = None
    source_10_mix_rate: Optional[float] = None
    mix_rate: Optional[float] = None
    target_01_mix_rate: Optional[float] = None
    target_10_mix_rate: Optional[float] = None
    aggregate_mix_rate: bool = False
    mix_rate_lower_bound: Optional[float] = 0.1
    target_01_mix_rate_lower_bound: Optional[float] = None
    target_10_mix_rate_lower_bound: Optional[float] = None
    pseudo_label_all_groups: bool = False
    shuffle_target: bool = True
    inbalance_ratio: Optional[bool] = False
    lr: float = 1e-4
    weight_decay: float = 1e-3 # 1e-4
    optimizer: str = "adamw"
    lr_scheduler: Optional[str] = None 
    num_cycles: float = 0.5
    frac_warmup: float = 0.05
    max_length: int = 128
    num_workers: int = 4
    freeze_heads: bool = False
    head_1_epochs: int = 5
    dataset_length: Optional[int] = None
    device: str = "cuda" if torch.cuda.is_available() else ("mps" if torch.backends.mps.is_available() else "cpu")
    exp_dir: str = f"output/{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}"
    plot_activations: bool = False

def post_init(conf: Config, overrides: list[str]=[]):
    if conf.target_01_mix_rate is not None and conf.target_10_mix_rate is None:
        conf.target_10_mix_rate = 0.0
        if conf.mix_rate is None:
            conf.mix_rate = conf.target_01_mix_rate
        assert conf.mix_rate == conf.target_01_mix_rate
    elif conf.target_01_mix_rate is None and conf.target_10_mix_rate is not None:
        conf.target_01_mix_rate = 0.0
        if conf.mix_rate is None:
            conf.mix_rate = conf.target_10_mix_rate
        assert conf.mix_rate == conf.target_10_mix_rate
    elif conf.target_01_mix_rate is not None and conf.target_10_mix_rate is not None:
        if conf.mix_rate is None:
            conf.mix_rate = conf.target_01_mix_rate + conf.target_10_mix_rate
        assert conf.mix_rate == conf.target_01_mix_rate + conf.target_10_mix_rate
    else: # both are none 
        if conf.mix_rate is not None:
            conf.target_01_mix_rate = conf.mix_rate / 2
            conf.target_10_mix_rate = conf.mix_rate / 2

    if conf.freeze_heads and "head_1_epochs" not in overrides:
        conf.head_1_epochs = round(conf.epochs / 2)
    
    if conf.source_mix_rate is not None:
        conf.source_01_mix_rate = conf.source_mix_rate / 2
        conf.source_10_mix_rate = conf.source_mix_rate / 2
    

    if conf.mix_rate_lower_bound is None:
        conf.mix_rate_lower_bound = conf.mix_rate

    if conf.target_01_mix_rate_lower_bound is None and conf.target_10_mix_rate_lower_bound is None and conf.mix_rate_lower_bound is not None:
        conf.target_01_mix_rate_lower_bound = conf.mix_rate_lower_bound / 2
        conf.target_10_mix_rate_lower_bound = conf.mix_rate_lower_bound / 2


# In[ ]:


conf = Config()


# In[ ]:


# if conf.dataset in ["waterbirds", "celebA-0", "celebA-1", "celebA-2", "toy_grid"]:
#     if conf.loss_type == LossType.TOPK and conf.mix_rate_lower_bound == 0.1:
#         conf.aux_weight = 7.0 
#     else:
#         conf.aux_weight = 2.0


# In[ ]:


# if conf.dataset == "toy_grid": 
#     conf.lr = 1e-3
#     conf.optimizer = "sgd"
#     conf.model = "toy_model"
#     conf.epochs = 128


# In[ ]:


# if conf.loss_type == LossType.DBAT:
#     conf.shared_backbone = False 
#     conf.freeze_heads = True
#     conf.batch_size = 16
#     conf.target_batch_size = 32


# In[ ]:


# if conf.dataset in ["waterbirds", "cub"] and conf.loss_type == LossType.DIVDIS:
#     conf.lr = 1e-3
#     conf.weight_decay = 1e-4
#     conf.epochs = 100
#     conf.optimizer = "sgd"
#     conf.batch_size = 16 
#     conf.target_batch_size = 16
#     conf.aux_weight = 10.0
#     conf.shuffle_target = False



# In[ ]:


# if conf.dataset == "waterbirds" and conf.loss_type == LossType.TOPK:
#     conf.optimizer = "sgd"
#     conf.target_01_mix_rate_lower_bound = 0.38
#     conf.target_10_mix_rate_lower_bound = 0.10
#     conf.mix_rate_lower_bound = None


# In[ ]:


# # # toy grid configs 
# if conf.dataset == "toy_grid":
#     conf.model = "toy_model"
#     conf.epochs = 128
# if conf.model == "ClipViT":
#     # conf.epochs = 5
#     conf.lr = 1e-5
# Resnet50 Configs
# if conf.model == "Resnet50":
#     conf.lr = 1e-4 # probably too high, should be 1e-4
# if conf.dataset == "multi_nli" or conf.dataset == "civil_comments":
#     conf.model = "bert"
#     conf.lr = 1e-5
#     conf.lr_scheduler = "cosine"



# In[ ]:


#get config overrides if runnign from command line
overrride_keys = []
if not is_notebook():
    import sys 
    overrides = OmegaConf.from_cli(sys.argv[1:])
    overrride_keys = overrides.keys()
    conf_dict = OmegaConf.merge(OmegaConf.structured(conf), overrides)
    conf = Config(**conf_dict)
post_init(conf, overrride_keys)


# In[ ]:


# create directory from config
from dataclasses import asdict
exp_dir = conf.exp_dir
os.makedirs(exp_dir, exist_ok=True)

# save full config to exp_dir
with open(f"{exp_dir}/config.yaml", "w") as f:
    OmegaConf.save(config=conf, f=f)


# In[ ]:


torch.manual_seed(conf.seed)
np.random.seed(conf.seed)


# In[ ]:


model_transform = None
pad_sides = False
tokenizer = None
if conf.model == "Resnet50":
    from torchvision import models
    from torchvision.models.resnet import ResNet50_Weights
    resnet_builder = lambda: models.resnet50(weights=ResNet50_Weights.IMAGENET1K_V1)    
    model_builder = lambda: torch.nn.Sequential(*list(resnet_builder().children())[:-1])
    resnet_50_transforms = ResNet50_Weights.IMAGENET1K_V1.transforms()
    model_transform = transforms.Compose([
        transforms.Resize(resnet_50_transforms.resize_size * 2, interpolation=resnet_50_transforms.interpolation),
        transforms.CenterCrop(resnet_50_transforms.crop_size),
        transforms.Normalize(mean=resnet_50_transforms.mean, std=resnet_50_transforms.std)
    ])
    pad_sides = True
    feature_dim = 2048
elif conf.model == "ClipViT":
    # from models.clip_vit import ClipViT
    # model_builder = lambda: ClipViT()
    # feature_dim = 768
    # input_size = 96
    # model_transform = transforms.Compose([
    #     transforms.Resize((224, 224), interpolation=transforms.InterpolationMode.BICUBIC)
    # ])
    import clip 
    preprocess = clip.clip._transform(224)
    clip_builder = lambda: clip.load('ViT-B/32', device='cpu')[0]
    model_builder = lambda: clip_builder().visual
    model_transform = transforms.Compose([
        preprocess.transforms[0],
        preprocess.transforms[1],
        preprocess.transforms[4]
    ])
    feature_dim = 512
    pad_sides = True
elif conf.model == "bert":
    from transformers import BertModel, BertTokenizer
    from models.hf_wrapper import HFWrapper
    bert_builder = lambda: BertModel.from_pretrained('bert-base-uncased')
    model_builder = lambda: HFWrapper(bert_builder())
    feature_dim = 768
    tokenizer = BertTokenizer.from_pretrained('bert-base-uncased')
elif conf.model == "toy_model":
    model_builder = lambda: nn.Sequential(
        nn.Linear(2, 40), nn.ReLU(), nn.Linear(40, 40), nn.ReLU()
    )
    feature_dim = 40
elif conf.model == "LeNet":
    from models.lenet import LeNet
    from functools import partial
    model_builder = lambda: partial(LeNet, num_classes=1, dropout_p=0.0)
    feature_dim = 256
else: 
    raise ValueError(f"Model {conf.model} not supported")


# In[ ]:


collate_fn = None
# TODO: there should be varaible n_classes for each feature 
classes = 2
n_features = 2 
is_img = True
alt_index = 1

if conf.dataset == "toy_grid":
    source_train, source_val, target_train, target_val, target_test = get_toy_grid_datasets(
        source_mix_rate_0_1=conf.source_01_mix_rate, 
        source_mix_rate_1_0=conf.source_10_mix_rate, 
        target_mix_rate_0_1=conf.target_01_mix_rate, 
        target_mix_rate_1_0=conf.target_10_mix_rate, 
    )
elif conf.dataset == "cifar_mnist":
    source_train, source_val, target_train, target_val, target_test = get_cifar_mnist_datasets(
        source_mix_rate_0_1=conf.source_01_mix_rate, 
        source_mix_rate_1_0=conf.source_10_mix_rate, 
        target_mix_rate_0_1=conf.target_01_mix_rate, 
        target_mix_rate_1_0=conf.target_10_mix_rate, 
        transform=model_transform, 
        pad_sides=pad_sides
    )

elif conf.dataset == "fmnist_mnist":
    source_train, source_val, target_train, target_val, target_test = get_fmnist_mnist_datasets(
        source_mix_rate_0_1=conf.source_01_mix_rate, 
        source_mix_rate_1_0=conf.source_10_mix_rate, 
        target_mix_rate_0_1=conf.target_01_mix_rate, 
        target_mix_rate_1_0=conf.target_10_mix_rate, 
        transform=model_transform, 
        pad_sides=pad_sides
    )
elif conf.dataset == "waterbirds":
    source_train, source_val, target_train, target_val, target_test = get_waterbirds_datasets(
        mix_rate=conf.mix_rate, 
        source_cc=conf.source_cc,
        transform=model_transform, 
        convert_to_tensor=True,
        val_split=conf.source_val_split,
        target_val_split=conf.target_val_split, 
        dataset_length=conf.dataset_length
    )
elif conf.dataset == "cub":
    source_train, target_train, target_test = get_cub_datasets()
    source_val = []
    target_val = []
elif conf.dataset.startswith("celebA"):
    if conf.dataset == "celebA-0":
        gt_feat = "Blond_Hair"
        spur_feat = "Male"
        inv_spur_feat = True
    elif conf.dataset == "celebA-1":
        gt_feat = "Mouth_Slightly_Open"
        spur_feat = "Wearing_Lipstick"
        inv_spur_feat = False
    elif conf.dataset == "celebA-2":
        gt_feat = "Wavy_Hair"
        spur_feat = "High_Cheekbones"
        inv_spur_feat = False
    else: 
        raise ValueError(f"Dataset {conf.dataset} not supported")
    source_train, source_val, target_train, target_val, target_test = get_celebA_datasets(
        mix_rate=conf.mix_rate, 
        source_cc=conf.source_cc,
        transform=model_transform, 
        gt_feat=gt_feat,
        spur_feat=spur_feat,
        inv_spur_feat=inv_spur_feat,
        dataset_length=conf.dataset_length
    )
elif conf.dataset == "camelyon":
    source_train, source_val, target_train, target_val, target_test = get_camelyon_datasets(
        transform=model_transform
    )
elif conf.dataset == "civil_comments":
    source_train, source_val, target_train, target_val, target_test = get_civil_comments_datasets(
        tokenizer=tokenizer,
        max_length=conf.max_length, 
        dataset_length=conf.dataset_length
    )
    is_img = False

elif conf.dataset == "multi_nli":
    source_train, source_val, target_train, target_val, target_test = get_multi_nli_datasets(
        mix_rate=conf.mix_rate,
        source_cc=conf.source_cc,
        tokenizer=tokenizer,
        max_length=conf.max_length, 
        dataset_length=conf.dataset_length
    )
    is_img = False

else:
    raise ValueError(f"Dataset {conf.dataset} not supported")

# if classes == 2 and conf.binary:
#     classes = 1



# In[ ]:


# plot image 
img, y, gl = source_train[-1]
# pad 
# to PIL image 

# img = transforms.ToPILImage()(img)
# img
if is_img and img.dim() == 3 and is_notebook():
    plt.imshow(img.permute(1, 2, 0))
    # show without axis 
    plt.axis('off')
    plt.show()


# In[ ]:


# plot target train images with vision_utils.make_grid
if is_img and img.dim() == 3 and is_notebook():
    img_tensor_grid = torch.stack([target_train[i][0] for i in range(20)])
    grid_img = vision_utils.make_grid(img_tensor_grid, nrow=10, normalize=True, padding=1)
    plt.imshow(grid_img.permute(1, 2, 0))
    plt.show()


# In[ ]:


class DivisibleBatchSampler(torch.utils.data.Sampler):
    def __init__(self, dataset_size: int, batch_size: int, shuffle: bool = True):
        self.dataset_size = dataset_size
        self.batch_size = batch_size
        self.shuffle = shuffle
        self.rng = rnd.Random(42)
        
        # Calculate number of complete batches and total samples needed
        self.num_batches = math.ceil(dataset_size / batch_size)
        self.total_size = self.num_batches * batch_size

    def __iter__(self):
        # Generate indices for the entire dataset
        indices = list(range(self.dataset_size))
        
        if self.shuffle:
            # Shuffle all indices
            self.rng.shuffle(indices)
            
        # If we need more indices to make complete batches,
        # randomly sample from existing indices
        if self.total_size > self.dataset_size:
            extra_indices = self.rng.choices(indices, k=self.total_size - self.dataset_size)
            indices.extend(extra_indices)
            
        assert len(indices) == self.total_size
        return iter(indices)

    def __len__(self):
        return self.total_size


# In[ ]:


source_train_loader = DataLoader(
    source_train, batch_size=conf.batch_size, num_workers=conf.num_workers, 
    sampler=DivisibleBatchSampler(len(source_train), conf.batch_size, shuffle=True), 
)
if len(source_val) > 0:
    source_val_loader = DataLoader(
        source_val, batch_size=conf.batch_size, num_workers=conf.num_workers, 
        sampler=DivisibleBatchSampler(len(source_val), conf.batch_size, shuffle=False)
    )
# NOTE: shuffle "should" be true, but in divdis code its false, and this leads to substantial changes in worst goup result
target_train_loader = DataLoader(
    target_train, batch_size=conf.target_batch_size, num_workers=conf.num_workers, 
    sampler=DivisibleBatchSampler(len(target_train), conf.target_batch_size, shuffle=conf.shuffle_target)
)
if len(target_val) > 0:
    target_val_loader = DataLoader(
        target_val, batch_size=conf.target_batch_size, num_workers=conf.num_workers, 
        sampler=DivisibleBatchSampler(len(target_val), conf.target_batch_size, shuffle=False)
    )
target_test_loader = DataLoader(
    target_test, batch_size=conf.batch_size, num_workers=conf.num_workers, shuffle=False
)

# classifiers
from transformers import get_cosine_schedule_with_warmup
if conf.shared_backbone:
    net = MultiHeadBackbone(model_builder(), conf.heads, feature_dim, classes if not conf.binary else 1)
else:
    print("warning, not using shared backbone untested")
    net = MultiNetModel(model_builder=model_builder, n_heads=conf.heads, feature_dim=feature_dim, classes=classes)
net = net.to(conf.device)

# optimizer
if conf.optimizer == "adamw":
    opt = torch.optim.AdamW(net.parameters(), lr=conf.lr, weight_decay=conf.weight_decay)
elif conf.optimizer == "sgd":
    opt = torch.optim.SGD(net.parameters(), lr=conf.lr, weight_decay=conf.weight_decay, momentum=0.9)
else: 
    raise ValueError(f"Optimizer {conf.optimizer} not supported")
num_steps = conf.epochs * len(source_train_loader)
if conf.lr_scheduler == "cosine":       
    scheduler = get_cosine_schedule_with_warmup(
        opt, 
        num_warmup_steps=num_steps * conf.frac_warmup, 
        num_training_steps=num_steps, 
        num_cycles=conf.num_cycles
    )
else: 
    # constant learning rate
    scheduler = None

# loss function
if conf.loss_type == LossType.DIVDIS:
    loss_fn = DivDisLoss(heads=conf.heads)
elif conf.loss_type == LossType.DBAT:
    loss_fn = DBatLoss(heads=conf.heads)
elif conf.loss_type == LossType.CONF:
    loss_fn = ConfLoss()
elif conf.loss_type == LossType.SMOOTH:
    loss_fn = SmoothTopLoss(
        criterion=partial(F.binary_cross_entropy_with_logits, reduction='none'), 
        device=conf.device
    )
elif conf.loss_type == LossType.ERM:
    loss_fn = PassThroughLoss()
elif conf.loss_type in [LossType.TOPK, LossType.EXP, LossType.PROB]:
    if conf.aggregate_mix_rate:
        mix_rate = conf.mix_rate_lower_bound 
        group_mix_rates = None
    else:
        mix_rate = None 
        group_mix_rates = {(0, 1): conf.target_01_mix_rate_lower_bound, (1, 0): conf.target_10_mix_rate_lower_bound}
    loss_fn = ACELoss(
        heads=conf.heads, 
        classes=classes,
        binary=conf.binary,
        mode=conf.loss_type.value, 
        inbalance_ratio=conf.inbalance_ratio,
        mix_rate=mix_rate,
        group_mix_rates=group_mix_rates,
        pseudo_label_all_groups=conf.pseudo_label_all_groups,
        device=conf.device
    )
else:
    raise ValueError(f"Loss type {conf.loss_type} not supported")


# # Plot Activations

# In[ ]:


# visualize data using first two principle componets of final layer activations
if is_notebook() and conf.plot_activations:
    model = model_builder()
    model = model.to(conf.device)
    activations, labels = get_acts_and_labels(model, target_test_loader)
    activations_pca, pca = transform_activations(activations)


# In[ ]:


if is_notebook() and conf.plot_activations:
    group_labels = labels[:, 0] * 2 + labels[:, 1]
    plt.scatter(activations_pca[:, 0], activations_pca[:, 1], c=group_labels.to('cpu'), cmap="viridis")
    plt.title("Group labels")
    plt.show()


# In[ ]:


from sklearn.linear_model import LogisticRegression
if is_notebook() and conf.plot_activations:
    component_range = [2**i for i in range(1, 9)]
    component_range = [i for i in component_range if i <= feature_dim]
    n_components_accs = []
    for n_components in tqdm(component_range):
        pca = PCA(n_components=n_components)
        pca.fit(activations)
        activations_pca = pca.transform(activations)
        # fit probe 
        lr = LogisticRegression(max_iter=1000)
        lr.fit(activations_pca, labels[:, 0].to('cpu').numpy())
        acc = lr.score(activations_pca, labels[:, 0].to('cpu').numpy())
        n_components_accs.append(acc)
    plt.plot(component_range, n_components_accs, label="accuracy")
    plt.show()



# In[ ]:


# fit linear probe 
if is_notebook() and conf.plot_activations:
    from sklearn.linear_model import LogisticRegression
    lr = LogisticRegression(max_iter=10000)
    lr.fit(activations.to('cpu').numpy(), labels[:, 0].to('cpu').numpy())
    # get accuracy 
    acc = lr.score(activations.to('cpu').numpy(), labels[:, 0].to('cpu').numpy())
    print(f"Accuracy: {acc:.4f}")


# In[ ]:


if is_notebook() and conf.plot_activations:
    fig = plt.figure(figsize=(12, 5))
    # Second 3D plot for group labels
    ax3 = fig.add_subplot(121, projection='3d')
    scatter3 = ax3.scatter(activations_pca[:, 0], activations_pca[:, 1], activations_pca[:,2], 
                        c=group_labels.to('cpu'), cmap="viridis")
    ax3.view_init(25, 210, 0)
    ax3.set_title('Group labels')


# In[ ]:


if not is_notebook() and conf.plot_activations:
    fig = plot_activations(model=net.backbone, loader=target_test_loader, device=conf.device)
    fig.savefig(f"{exp_dir}/activations_pretrain.png")


# # Train

# In[ ]:


def compute_src_losses(logits, y, gl):
    logits_chunked = torch.chunk(logits, conf.heads, dim=-1)
    labels = torch.cat([y, y], dim=-1) if not conf.use_group_labels else gl
    labels_chunked = torch.chunk(labels, conf.heads, dim=-1)

    if conf.binary:
        losses = [F.binary_cross_entropy_with_logits(logit.squeeze(), y.squeeze().to(torch.float32)) for logit, y in zip(logits_chunked, labels_chunked)]
    else:
        assert logits_chunked[0].shape == (logits.size(0), classes), logits_chunked[0].shape
        losses = [F.cross_entropy(logit.squeeze(), y.squeeze().to(torch.long)) 
                  for logit, y in zip(logits_chunked, labels_chunked)]
    return losses

def compute_corrects(logits: torch.Tensor, head: int, y: torch.Tensor, binary: bool):
    if binary:
        return ((logits[:, head] > 0) == y.flatten()).sum().item()
    else:
        logits = logits.view(logits.size(0), conf.heads, classes)
        return (logits[:, head].argmax(dim=-1) == y).sum().item()
        


# In[ ]:


from torch.utils.tensorboard import SummaryWriter
class Logger():
    def __init__(self, exp_dir):
        self.exp_dir = exp_dir
        self.metrics = defaultdict(list)
        self.tb_writer = SummaryWriter(log_dir=exp_dir)
    
    def add_scalar(self, parition, name, value, step=None, to_metrics=True, to_tb=True):
        if to_metrics:
            self.metrics[f"{parition}_{name}"].append(value)
        if to_tb:
            self.tb_writer.add_scalar(f"{parition}/{name}", value, step)

    def flush(self):
        self.tb_writer.flush()
        # save metrics to json
        with open(f"{self.exp_dir}/metrics.json", "w") as f:
            json.dump(self.metrics, f, indent=4)


# In[ ]:


from itertools import product

def eval(model, loader, device, loss_fn, use_labels=False, stage: str = "Evaluating"): 
    group_label_ls = list(product(range(classes), repeat=n_features))

    # loss 
    losses = []

    # accuracy 
    total_corrects_by_groups = {
        group_label: torch.zeros(conf.heads)
        for group_label in group_label_ls
    }
    total_corrects_alt_by_groups = {
        group_label: torch.zeros(conf.heads)
        for group_label in group_label_ls
    }
    total_samples = 0
    total_samples_by_groups = {
        group_label: 0
        for group_label in group_label_ls
    }
    
    with torch.no_grad():
        for x, y, gl in tqdm(loader, desc=stage):
            x, y, gl = to_device(x, y, gl, conf.device)
            logits = model(x)
            # print(test_logits.shape)
            total_samples += logits.size(0)
            if use_labels and loss_fn is not None:
                loss = loss_fn(logits, y, gl)
            elif loss_fn is not None:
                loss = loss_fn(logits)
            else: 
                loss = torch.tensor(0.0, device=conf.device)
            if torch.isnan(loss):
                print(f"Warning: Nan Loss (likely due to batch size and target loss) "
                f"Batch size: {logits.size(0)}, Loss: {conf.loss_type}")
            else:
                losses.append(loss)

            # parition instances into groups based on group labels 
            logits_by_group = {}
            for group_label in group_label_ls:
                group_label_mask = torch.all(gl == torch.tensor(group_label).to(device), dim=1)
                # print("group label mask", group_label_mask.shape)
                logits_by_group[group_label] = logits[group_label_mask]
            # print("group logit shapes", [v.shape for v in logits_by_group.values()])
            
            for group_label, group_logits in logits_by_group.items():
                num_examples_group = group_logits.size(0)
                total_samples_by_groups[group_label] += num_examples_group
                group_labels = torch.tensor(group_label).repeat(num_examples_group, 1).to(device)
                # print("group label shapes", group_labels[:, 0].shape)
                for i in range(conf.heads):
                    total_corrects_by_groups[group_label][i] += compute_corrects(group_logits, i, group_labels[:, 0], conf.binary)
                    total_corrects_alt_by_groups[group_label][i] += compute_corrects(group_logits, i, group_labels[:, 1], conf.binary)
    
    total_corrects = torch.stack([gl_corrects for gl_corrects in total_corrects_by_groups.values()], dim=0).sum(dim=0)
    total_corrects_alt = torch.stack([gl_corrects for gl_corrects in total_corrects_alt_by_groups.values()], dim=0).sum(dim=0)

    # average metrics
    metrics = {}
    for i in range(conf.heads):
        metrics[f"acc_{i}"] = (total_corrects[i] / total_samples).item()
        metrics[f"acc_alt_{i}"] = (total_corrects_alt[i] / total_samples).item()
    
    if loss_fn is not None:
        metrics["loss"] = torch.mean(torch.tensor(losses)).item()
    # group acc per head
    for group_label in group_label_ls:
        for i in range(conf.heads): 
            metrics[f"acc_{i}_{group_label}"] = (total_corrects_by_groups[group_label][i] / total_samples_by_groups[group_label]).item()
            metrics[f"acc_alt_{i}_{group_label}"] = (total_corrects_alt_by_groups[group_label][i] / total_samples_by_groups[group_label]).item()
    # worst group acc per head
    for i in range(conf.heads):
        metrics[f"worst_acc_{i}"] = min([metrics[f"acc_{i}_{group_label}"] for group_label in group_label_ls])

    # add group counts 
    for group_label in group_label_ls:
        metrics[f"count_{group_label}"] = total_samples_by_groups[group_label]

    return metrics


# In[ ]:


# TODO: change diciotary values to source loss, target loss
from itertools import cycle
logger = Logger(exp_dir)
try:
    if conf.freeze_heads:
        # freeze second head (for dbat)
        net.freeze_head(1)
    for epoch in range(conf.epochs):
        train_loader = zip(source_train_loader, cycle(target_train_loader))
        loader_len = len(source_train_loader)
        # train
        for batch_idx, (source_batch, target_batch) in tqdm(enumerate(train_loader), desc="Source train", total=loader_len):
            # freeze heads for dbat
            if conf.freeze_heads and epoch == conf.head_1_epochs: 
                net.unfreeze_head(1)
                net.freeze_head(0)
            
            # source
            x, y, gl = to_device(*source_batch, conf.device)
            logits = net(x)
            losses = compute_src_losses(logits, y, gl)
            xent = torch.mean(torch.stack(losses))
            logger.add_scalar("train", "source_loss", xent.item(), epoch * loader_len + batch_idx)
            
            # target
            target_x, target_y, target_gl = to_device(*target_batch, conf.device)
            target_logits = net(target_x)
            target_loss = loss_fn(target_logits)
            logger.add_scalar("train", "target_loss", target_loss.item(), epoch * loader_len + batch_idx)
            logger.add_scalar("train", "weighted_target_loss", conf.aux_weight * target_loss.item(), epoch * loader_len + batch_idx, to_metrics=False, to_tb=True)
            # don't compute target loss before second head begins training
            if conf.freeze_heads and epoch < conf.head_1_epochs: 
                target_loss = torch.tensor(0.0, device=conf.device)
            
            # full loss 
            full_loss = conf.source_weight * xent + conf.aux_weight * target_loss
            logger.add_scalar("train", "loss", full_loss.item(), epoch * loader_len + batch_idx)
            
            # backprop
            opt.zero_grad()
            full_loss.backward()
            opt.step()
            if scheduler is not None:
                scheduler.step()
        
        # eval
        if (epoch + 1) % 1 == 0:
            net.eval()
            ### Validation 
            # source
            total_val_loss = 0.0
            total_val_weighted_loss = 0.0
            if len(source_val) > 0:
                src_loss_fn = lambda x, y, gl: sum(compute_src_losses(x, y, gl))
                source_val_metrics = eval(net, source_val_loader, conf.device, src_loss_fn, use_labels=True, stage="Source Val")
                for k, v in source_val_metrics.items():
                    if 'count' not in k:
                        logger.add_scalar("val", f"source_{k}", v, epoch)
                total_val_loss += source_val_metrics["loss"]
                total_val_weighted_loss += total_val_loss
            # target
            weighted_target_val_loss = 0.0
            if len(target_val) > 0:  
                target_val_metrics = eval(net, target_val_loader, conf.device, loss_fn, use_labels=False, stage="Target Val")
                for k, v in target_val_metrics.items():
                    if 'count' not in k:
                        logger.add_scalar("val", f"target_{k}", v, epoch)
                weighted_target_val_loss = target_val_metrics["loss"] * conf.aux_weight
                logger.add_scalar("val", "target_weighted_loss", weighted_target_val_loss, epoch)
                total_val_loss += target_val_metrics["loss"]    
                total_val_weighted_loss += weighted_target_val_loss
            # total
            logger.add_scalar("val", "loss", total_val_loss, epoch)
            logger.add_scalar("val", "weighted_loss", total_val_weighted_loss, epoch)

            ### Test
            target_test_metrics = eval(net, target_test_loader, conf.device, None, use_labels=False, stage="Target Test")
            for k, v in target_test_metrics.items():
                if 'count' not in k:
                    logger.add_scalar("test", k, v, epoch)


            ### Print Results
            print(f"Epoch {epoch + 1} Eval Results:")
            # print validation losses
            if len(source_val) > 0:
                print(f"Source validation loss: {logger.metrics['val_source_loss'][-1]:.4f}")
            if len(target_val) > 0:
                print(f"Target validation loss {logger.metrics['val_target_loss'][-1]:.4f}")
            print(f"Validation loss: {logger.metrics['val_loss'][-1]:.4f}")
            # if len(target_val) > 0:
            #     for group_label in product(range(2), repeat=gl.shape[1]):
            #         print(f"Group {group_label} validation count: {target_val_metrics[f'count_{group_label}']}")
            # print test accuracies (total and group)
            print("\n=== Test Accuracies ===")
            # Overall accuracy for each head
            print("\nOverall Accuracies:")
            for i in range(conf.heads):
                print(f"Head {i}:  Main: {logger.metrics[f'test_acc_{i}'][-1]:.4f}  |  Alt: {logger.metrics[f'test_acc_alt_{i}'][-1]:.4f}")
            # Worst group accuracy for each head
            print("\nWorst Group Accuracies:")
            for i in range(conf.heads):
                print(f"Head {i}:  Worst: {logger.metrics[f'test_worst_acc_{i}'][-1]:.4f}")
            # Group-wise accuracies
            print("\nGroup-wise Accuracies:")
            for group_label in product(range(2), repeat=gl.shape[1]):
                print(f"\nGroup {group_label}, count: {target_test_metrics[f'count_{group_label}']}:")
                for i in range(conf.heads):
                    print(f"Head {i}:  Main: {logger.metrics[f'test_acc_{i}_{group_label}'][-1]:.4f}  |  Alt: {logger.metrics[f'test_acc_alt_{i}_{group_label}'][-1]:.4f}")

            # plot activations 
            if conf.plot_activations:   
                fig = plot_activations(
                    model=net.backbone, loader=target_test_loader, device=conf.device
                )
                fig.savefig(f"{exp_dir}/activations_{epoch}.png")
                plt.close()
            
            net.train()
finally:
    logger.flush()

