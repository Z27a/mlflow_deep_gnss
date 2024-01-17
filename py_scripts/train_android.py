########################################################################
# Author(s):    Shubh Gupta, Ashwin Kanhere
# Date:         21 September 2021
# Desc:         Train a DNN to output position corrections for Android
#               measurements
########################################################################
import sys, os, csv, datetime
from typing import Dict
parent_directory = os.path.split(os.getcwd())[0]
src_directory = os.path.join(parent_directory, 'src')
data_directory = os.path.join(parent_directory, 'data')
ephemeris_data_directory = os.path.join(data_directory, 'ephemeris')
sys.path.insert(0, src_directory)
from mpl_toolkits.mplot3d import Axes3D
from sklearn.preprocessing import StandardScaler
import matplotlib.pyplot as plt # plotting
import numpy as np # linear algebra
import pandas as pd # data processing, CSV file I/O (e.g. pd.read_csv)
import torch
from torch.utils.data import DataLoader
import torch.nn.functional as F
import torch.nn as nn
from tqdm import tqdm
from torch.utils.tensorboard import SummaryWriter
import hydra
import mlflow
from omegaconf import DictConfig, OmegaConf

import gnss_lib.coordinates as coord
import gnss_lib.read_nmea as nmea
import gnss_lib.utils as utils 
import gnss_lib.solve_pos as solve_pos
from correction_network.android_dataset import Android_GNSS_Dataset
from correction_network.networks import Net_Snapshot, DeepSetModel

def collate_feat(batch):
    sorted_batch = sorted(batch, key=lambda x: x['features'].shape[0], reverse=True)
    features = [x['features'] for x in sorted_batch]
    features_padded = torch.nn.utils.rnn.pad_sequence(features)
    L, N, dim = features_padded.size()
    pad_mask = np.zeros((N, L))
    for i, x in enumerate(features):
        pad_mask[i, len(x):] = 1
    pad_mask = torch.Tensor(pad_mask).bool()
    correction = torch.Tensor([x['true_correction'] for x in sorted_batch])
    guess = torch.Tensor([x['guess'] for x in sorted_batch])
    retval = {
            'features': features_padded,
            'true_correction': correction,
            'guess': guess
        }
    return retval, pad_mask


def test_eval(val_loader, net, loss_func):
    # VALIDATION EVALUATION
    stats_val = []
    loss_val = 0
    generator = iter(val_loader)
    for i in tqdm(range(100), desc='test', leave=False):
        try:
            sample_batched = next(generator)
        except StopIteration:
            generator = iter(val_loader)
            sample_batched = next(generator)
        _sample_batched, pad_mask = sample_batched
    #         feat_pack = torch.nn.utils.rnn.pack_padded_sequence(_sample_batched['features'], x_lengths)
        x = _sample_batched['features'].float().cuda()
        y = _sample_batched['true_correction'].float().cuda()
        pad_mask = pad_mask.cuda()
        pred_correction = net(x, pad_mask=pad_mask)
        loss = loss_func(pred_correction, y)
        loss_val += loss
        stats_val.append((y-pred_correction).cpu().detach().numpy())
    return np.mean(np.abs(np.array(stats_val)), axis=0), loss_val/len(stats_val)


@hydra.main(config_path="../config", config_name="train_android_conf")
def main(config: DictConfig) -> None:
    data_config = {
    "root": data_directory,
    "raw_data_dir" : config.raw_data_dir,
    "data_dir": config.data_dir,
    # "initialization_dir" : "initialization_data",
    # "info_path": "data_info.csv",
    "max_open_files": config.max_open_files,
    "guess_range": [config.pos_range_xy, config.pos_range_xy, config.pos_range_z, config.clk_range, config.vel_range_xy, config.vel_range_xy, config.vel_range_z, config.clkd_range],
    "history": config.history,
    "seed": config.seed,
    "chunk_size": config.chunk_size,
    "max_sats": config.max_sats,
    "bias_fname": config.bias_fname,
    }
    
    mlflow.set_tracking_uri(uri="http://ras-b2-ph.nexus.csiro.au:5000")
    mlflow.set_experiment("Deep GNSS Leo test")

    print('Initializing dataset')
    
    dataset = Android_GNSS_Dataset(data_config)


    train_set, val_set = torch.utils.data.random_split(dataset, [int(config.frac*len(dataset)), len(dataset) - int(config.frac*len(dataset))])
    dataloader = DataLoader(train_set, batch_size=config.batch_size,
                            shuffle=True, num_workers=config.num_workers, collate_fn=collate_feat)
    val_loader = DataLoader(val_set, batch_size=1, 
                            shuffle=False, num_workers=0, collate_fn=collate_feat)
    print('Initializing network: ', config.model_name)
    if config.model_name == "set_transformer":
        net = Net_Snapshot(train_set[0]['features'].size()[1], 1, len(train_set[0]['true_correction']))     # define the network
    elif config.model_name == "deepsets":
        net = DeepSetModel(train_set[0]['features'].size()[1], len(train_set[0]['true_correction']))
    else:
        raise ValueError('This model is not supported yet!')
    
    if not config.resume==0:
        net.load_state_dict(torch.load(os.path.join(data_directory, 'weights', config.resume)))
        print("Resumed: ", config.resume)
    
    net.cuda()

    optimizer = torch.optim.Adam(net.parameters(), config.learning_rate)
    loss_func = torch.nn.MSELoss()
    count = 0
    fname = "android_" + config.prefix + "_"+ datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    if config.writer:
        writer = SummaryWriter(os.path.join(data_directory, 'runs', fname))

    input_eg = None

    min_acc = 1000000
    for epoch in range(config.N_train_epochs):
        # TRAIN Phase
        net.train()
        for i, sample_batched in enumerate(dataloader):
            _sample_batched, pad_mask = sample_batched
            
            x = _sample_batched['features'].float().cuda()
            y = _sample_batched['true_correction'].float().cuda()
            pad_mask = pad_mask.cuda()
            if input_eg is None:
                input_eg = _sample_batched['features'].float().numpy()
            pred_correction = net(x, pad_mask=pad_mask)
            loss = loss_func(pred_correction, y)
            mlflow.log_metrics({"train_loss": loss})
            if config.writer:
                writer.add_scalar("Loss/train", loss, count)
                
            count += 1    
            
            optimizer.zero_grad()   # clear gradients for next train
            loss.backward()         # backpropagation, compute gradients
            optimizer.step()        # apply gradients
        # TEST Phase
        net.eval()
        mean_acc, test_loss = test_eval(val_loader, net, loss_func)
        if config.writer:
            writer.add_scalar("Loss/test", test_loss, epoch)
            mlflow.log_metrics({"test_loss": test_loss})
        for j in range(len(mean_acc[0])):
            if config.writer:
                writer.add_scalar("Metrics/Acc_"+str(j), mean_acc[0, j], epoch)
                mlflow.log_metrics({"test_acc": mean_acc[0, j]})
        if np.sum(mean_acc) < min_acc:
            min_acc = np.sum(mean_acc)
            torch.save(net.state_dict(), os.path.join(data_directory, 'weights', fname))
        print('Training done for ', epoch)

    mlflow.pytorch.log_model(net, "model", input_example=input_eg, conda_env=f"{parent_directory}/environment.yml", code_paths=[f"{parent_directory}/src/correction_network"])

if __name__=="__main__":
    main()