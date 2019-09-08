#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Module for training and generating data from conditional and joint distributions
using WGANs.

Author: Jonas Metzger and Evan Munro
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils import data as D
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from hypergrad import AdamHD
from time import time


class DataWrapper(object):
    """Class for processing raw training data for training Wasserstein GAN

    Parameters
    ----------
    df: pandas.DataFrame
        Training data frame, includes both variables to be generated, and
        variables to be conditioned on
    continuous_vars: list
        List of str of continuous variables to be generated
    categorical_vars: list
        List of str of categorical variables to be generated
    context_vars: list
        List of str of variables that are conditioned on for cWGAN
    continuous_lower_bounds: dict
        Key is element of continuous_vars, value is lower limit on that variable.
    continuous_upper_bounds: dict
        Key is element of continuous_vars, value is upper limit on that variable.

    Attributes
    ----------
    variables: dict
        Includes lists of names of continuous, categorical and context variables
    means: list
        List of means of continuous and context variables
    stds: list
        List of float of standard deviation of continuous and context variables
    cat_dims: list
        List of dimension of each categorical variable
    cont_bounds: torch.tensor
        formatted lower and upper bounds of continuous variables
    """
    def __init__(self, df, continuous_vars=[], categorical_vars=[], context_vars=[],
                 continuous_lower_bounds = dict(), continuous_upper_bounds = dict()):
        variables = dict(continuous=continuous_vars,
                         categorical=categorical_vars,
                         context=context_vars)
        self.variables = variables
        continuous, context = [torch.tensor(np.array(df[variables[_]])).to(torch.float) for _ in ("continuous", "context")]
        self.means = [x.mean(0, keepdim=True) for x in (continuous, context)]
        self.stds  = [x.std(0,  keepdim=True) + 1e-5 for x in (continuous, context)]
        self.cat_dims = [df[v].nunique() for v in variables["categorical"]]
        self.cont_bounds = [[continuous_lower_bounds[v] if v in continuous_lower_bounds.keys() else -1e8 for v in variables["continuous"]],
                            [continuous_upper_bounds[v] if v in continuous_upper_bounds.keys() else 1e8 for v in variables["continuous"]]]
        self.cont_bounds = (torch.tensor(self.cont_bounds).to(torch.float) - self.means[0]) / self.stds[0]

    def preprocess(self, df):
        """
        Scale training data for training in WGANs

        Parameters
        ----------
        df: pandas.DataFrame
            raw training data
        Returns
        -------
        x: torch.tensor
            training data to be generated by WGAN

        context: torch.tensor
            training data to be conditioned on by WGAN
        """
        continuous, context = [torch.tensor(np.array(df[self.variables[_]])).to(torch.float) for _ in ("continuous", "context")]
        continuous, context = [(x-m)/s for x,m,s in zip([continuous, context], self.means, self.stds)]
        if len(self.variables["categorical"]) > 0:
            categorical = torch.tensor(pd.get_dummies(df[self.variables["categorical"]], columns=self.variables["categorical"]).to_numpy())
            return torch.cat([continuous, categorical.to(torch.float)], -1), context
        else:
            return continuous, context

    def deprocess(self, x, context):
        """
        Unscale tensors from WGAN output to original scale

        Parameters
        ----------
        x: torch.tensor
            Generated data
        context: torch.tensor
            Data conditioned on
        Returns
        -------
        df: pandas.DataFrame
            DataFrame with data converted back to original scale
        """
        continuous, categorical = x.split((self.means[0].size(-1), sum(self.cat_dims)), -1)
        continuous, context = [x*s+m for x,m,s in zip([continuous, context], self.means, self.stds)]
        if categorical.size(-1) > 0: categorical = torch.cat([torch.multinomial(p, 1) for p in categorical.split(self.cat_dims, -1)], -1)
        df = pd.DataFrame(dict(zip(self.variables["continuous"] + self.variables["categorical"] + self.variables["context"],
                                   torch.cat([continuous, categorical.to(torch.float), context], -1).detach().t())))
        return df

    def apply_generator(self, generator, df):
        """
        Replaces columns in DataFrame that are generated by the generator, of
        size equal to the number of rows in the DataFrame that is passed

        Parameters
        ----------
        df: pandas.DataFrame
            Must contain columns generated by the generator,
            listed in self.variables["continuous"] and
            self.variables["categorical"]
        generator: wgan_model.Generator
            Trained generator for simulating data
        Returns
        -------
        pandas.DataFrame
            Original DataFrame with columns replaced by generated data where possible.
        """
        # replaces columns in df with data from generator wherever possible
        generator.to("cpu")
        original_columns = df.columns
        x, context = self.preprocess(df)
        x_hat = generator(context)
        df_hat = self.deprocess(x_hat, context)
        updated = self.variables["continuous"] + self.variables["categorical"]
        not_updated = [col for col in list(df_hat.columns) if col not in updated]
        df_hat = df_hat.drop(not_updated, axis=1).reset_index(drop=True)
        df = df.drop(updated, axis=1).reset_index(drop=True)
        return df_hat.join(df)[original_columns]

    def apply_critic(self, critic, df, colname="critic"):
        """
        Adds column with critic output for each row the provided Dataframe

        Parameters
        ----------
        critic: wgan_model.Critic
        df: pandas.DataFrame
        colname: str
            Name of column to add to df with critic output value
        Returns
        -------
        pandas.DataFrame
        """
        critic.to("cpu")
        x, context = self.preprocess(df)
        c = critic(x, context).detach()
        if colname in list(df.columns): df = df.drop(colname, axis=1)
        df.insert(0, colname, c[:, 0].numpy())
        return df


class Specifications(object):
    """Class used to set up WGAN training specifications before training
    Generator and Critic.

    Parameters
    ----------
    data_wrapper: wgan_model.DataWrapper
        Object containing details on data frame to be trained
    critic_d_hidden: list
        List of int, length equal to the number of hidden layers in the critic,
        giving the size of each hidden layer.
    critic_dropout: float
        Dropout parameter for critic (see Srivastava et al 2014)
    critic_steps: int
        Number of critic training steps taken for each generator training step
    critic_lr: float
        Initial learning rate for critic
    critic_gp_factor: int
        Weight on gradient penalty for critic loss function
    generator_d_hidden: list
        List of int, length equal to the number of hidden layers in generator,
        giving the size of each hidden layer.
    generator_dropout: float
        Dropout parameter for generator (See Srivastava et al 2014)
    generator_lr: float
        Initial learning rate for generator
    generator_d_noise: int
        The dimension of the noise input to the generator. Default sets to the
        output dimension of the generator.
    optimizer: str
        The optimizer used for training the neural networks.
    max_epochs: int
        The number of times to train the network on the whole dataset.
    batch_size: int
        The batch size for each training iteration.
    test_set_size: int
        Holdout test set for calculating out of sample wasserstein distance.
    load_checkpoint: str
        Filepath to existing model weights to start training from.
    save_checkpoint: str
        Filepath of folder to save model weights every save_every iterations
    save_every: int
        If save_checkpoint is not None, then how often to save checkpoint of model
        weights during training.
    print_every: int
        How often to print training status during training.
    device: str
        Either "cuda" if GPU is available or "cpu" if not

    Attributes
    ----------
    settings: dict
        Contains the neural network-related settings for training
    data: dict
        Contains settings related to the data dimension and bounds
    """
    def __init__(self, data_wrapper,
                 critic_d_hidden = [128,128,128],
                 critic_dropout = 0.1,
                 critic_steps = 15,
                 critic_lr = 1e-4,
                 critic_gp_factor = 5,
                 generator_d_hidden = [128,128,128],
                 generator_dropout = 0.1,
                 generator_lr = 1e-4,
                 generator_d_noise = "generator_d_output",
                 optimizer = "AdamHD",
                 max_epochs = 1000,
                 batch_size = 32,
                 test_set_size = 16,
                 load_checkpoint = None,
                 save_checkpoint = None,
                 save_every = 100,
                 print_every = 200,
                 device = "cuda" if torch.cuda.is_available() else "cpu"):

        self.settings = locals()
        del self.settings["self"], self.settings["data_wrapper"]
        d_context = len(data_wrapper.
                        variables["context"])
        d_cont = len(data_wrapper.variables["continuous"])
        d_x = d_cont + sum(data_wrapper.cat_dims)
        if generator_d_noise == "generator_d_output":
            self.settings.update(generator_d_noise = d_x)
        self.data = dict(d_context=d_context, d_x=d_x,
                         cat_dims=data_wrapper.cat_dims,
                         cont_bounds=data_wrapper.cont_bounds)

        print("settings:", self.settings)


class Generator(nn.Module):
    """
    torch.nn.Module class for generator network in WGAN

    Parameters
    ----------
    specifications: wgan_model.Specifications
        parameters for training WGAN

    Attributes
    ----------
    cont_bounds: torch.tensor
        formatted lower and upper bounds of continuous variables
    cat_dims: list
        Dimension of each categorical variable
    d_cont: int
        Total dimension of continuous variables
    d_cat: int
        Total dimension of categorical variables
    d_noise: int
        Dimension of noise input to generator
    layers: torch.nn.ModuleList
        Dense neural network layers making up the generator
    dropout: torch.nn.Dropout
        Dropout layer based on specifications

    """
    def __init__(self, specifications):
        super().__init__()
        s, d = specifications.settings, specifications.data
        self.cont_bounds = d["cont_bounds"]
        self.cat_dims = d["cat_dims"]
        self.d_cont = self.cont_bounds.size(-1)
        self.d_cat = sum(d["cat_dims"])
        self.d_noise = s["generator_d_noise"]
        d_in = [self.d_noise + d["d_context"]] + s["generator_d_hidden"]
        d_out = s["generator_d_hidden"] + [self.d_cont + self.d_cat]
        self.layers = nn.ModuleList([nn.Linear(i, o) for i, o in zip(d_in, d_out)])
        self.dropout = nn.Dropout(s["generator_dropout"])

    def _transform(self, hidden):
        continuous, categorical = hidden.split([self.d_cont, self.d_cat], -1)
        # apply bounds to continuous
        bounds = self.cont_bounds.to(hidden.device)
        continuous = torch.stack([continuous, bounds[0:1].expand_as(continuous)]).max(0).values
        continuous = torch.stack([continuous, bounds[1:2].expand_as(continuous)]).min(0).values
        # renormalize categorical
        if categorical.size(-1) > 0: categorical = torch.cat([F.softmax(x, -1) for x in categorical.split(self.cat_dims, -1)], -1)
        return torch.cat([continuous, categorical], -1)

    def forward(self, context):
        """
            Run generator model

        Parameters
        ----------
        context: torch.tensor
            Variables to condition on

        Returns
        -------
        torch.tensor
        """
        noise = torch.randn(context.size(0), self.d_noise).to(context.device)
        x = torch.cat([noise, context], -1)
        for layer in self.layers[:-1]:
            x = self.dropout(F.relu(layer(x)))
        return self._transform(self.layers[-1](x))


class Critic(nn.Module):
    """
    torch.nn.Module for critic in WGAN framework

    Parameters
    ----------
    specifications: wgan_model.Specifications

    Attributes
    ----------
    layers: torch.nn.ModuleList
        Dense neural network making up the critic
    dropout: torch.nn.Dropout
        Dropout layer applied between each of hidden layers
    """
    def __init__(self, specifications):
        super().__init__()
        s, d = specifications.settings, specifications.data
        d_in = [d["d_x"] + d["d_context"]] + s["critic_d_hidden"]
        d_out = s["critic_d_hidden"] + [1]
        self.layers = nn.ModuleList([nn.Linear(i, o) for i, o in zip(d_in, d_out)])
        self.dropout = nn.Dropout(s["critic_dropout"])

    def forward(self, x, context):
        """
        Run critic model

        Parameters
        ----------
        x: torch.tensor
            Real or generated data
        context: torch.tensor
            Data conditioned on

        Returns
        -------
        torch.tensor
        """
        x = torch.cat([x, context], -1)
        for layer in self.layers[:-1]:
            x = self.dropout(F.relu(layer(x)))
        return self.layers[-1](x)

    def gradient_penalty(self, x, x_hat, context):
        """
        Calculate gradient penalty

        Parameters
        ----------
        x: torch.tensor
            real data
        x_hat: torch.tensor
            generated data
        context: torch.tensor
            context data

        Returns
        -------
        torch.tensor
        """
        alpha = torch.randn(x.size(0)).unsqueeze(1).to(x.device)
        interpolated = x * alpha + x_hat * (1 - alpha)
        interpolated = torch.autograd.Variable(interpolated.detach(), requires_grad=True)
        critic = self(interpolated, context)
        gradients = torch.autograd.grad(critic, interpolated, torch.ones_like(critic),
                                        retain_graph=True, create_graph=True, only_inputs=True)[0]
        penalty = F.relu(gradients.norm(2, dim=1) - 1).mean()             # one-sided
        # penalty = (gradients.norm(2, dim=1) - 1).pow(2).mean()          # two-sided
        return penalty


def train(generator, critic, x, context, specifications):
    """
    Function for training generator and critic in conditional WGAN-GP
    If context is empty, trains a regular WGAN-GP. See Gulrajani et al 2017
    for details on training procedure.

    Parameters
    ----------
    generator: wgan_model.Generator
        Generator network to be trained
    critic: wgan_model.Critic
        Critic network to be trained
    x: torch.tensor
        Training data for generated data
    context: torch.tensor
        Data conditioned on for generating data
    specifications: wgan_model.Specifications
        Includes all the tuning parameters for training
    """
    # setup training objects
    s = specifications.settings
    start_epoch, step, description, device, t = 0, 1, "", s["device"], time()
    generator.to(device), critic.to(device)
    opt = {"AdamHD": AdamHD, "Adam": torch.optim.Adam}[s["optimizer"]]
    opt_generator = opt(generator.parameters(), lr=s["generator_lr"])
    opt_critic = opt(critic.parameters(), lr=s["critic_lr"])
    train_batches, test_batches = D.random_split(D.TensorDataset(x, context), (x.size(0)-s["test_set_size"], s["test_set_size"]))
    train_batches, test_batches = (D.DataLoader(d, s["batch_size"], shuffle=True) for d in (train_batches, test_batches))

    # load checkpoints
    if s["load_checkpoint"]:
        cp = torch.load(s["load_checkpoint"])
        generator.load_state_dict(cp["generator_state_dict"])
        opt_generator.load_state_dict(cp["opt_generator_state_dict"])
        critic.load_state_dict(cp["critic_state_dict"])
        opt_critic.load_state_dict(cp["opt_critic_state_dict"])
        start_epoch, step = cp["epoch"], cp["step"]
    # start training
    for epoch in range(start_epoch, s["max_epochs"]):
        # train loop
        WD_train, n_batches = 0, 0
        for x, context in train_batches:
            x, context = x.to(device), context.to(device)
            generator_update = step % s["critic_steps"] == 0
            for par in critic.parameters():
                par.requires_grad = not generator_update
            for par in generator.parameters():
                par.requires_grad = generator_update
            if generator_update:
                generator.zero_grad()
            else:
                critic.zero_grad()
            x_hat = generator(context)
            critic_x_hat = critic(x_hat, context).mean()
            if not generator_update:
                critic_x = critic(x, context).mean()
                WD = critic_x - critic_x_hat
                loss = - WD + s["critic_gp_factor"] * critic.gradient_penalty(x, x_hat, context)
                loss.backward()
                opt_critic.step()
                WD_train += WD.item()
                n_batches += 1
            else:
                loss = - critic_x_hat
                loss.backward()
                opt_generator.step()
            step += 1
        WD_train /= n_batches
        # test loop
        WD_test, n_batches = 0, 0
        for x, context in test_batches:
            x, context = x.to(device), context.to(device)
            with torch.no_grad():
                x_hat = generator(context)
                critic_x_hat = critic(x_hat, context).mean()
                critic_x = critic(x, context).mean()
                WD_test += (critic_x - critic_x_hat).item()
                n_batches += 1
        WD_test /= n_batches
        # diagnostics
        if epoch % s["print_every"] == 0:
            description = "epoch {} | step {} | WD_test {} | WD_train {} | sec passed {} |".format(
            epoch, step, round(WD_test, 2), round(WD_train, 2), round(time() - t))
            print(description)
            t = time()
        if s["save_checkpoint"] and epoch % s["save_every"] == 0:
            torch.save({"epoch": epoch, "step": step,
                        "generator_state_dict": generator.state_dict(),
                        "critic_state_dict": critic.state_dict(),
                        "opt_generator_state_dict": opt_generator.state_dict(),
                        "opt_critic_state_dict": opt_critic.state_dict()}, s["save_checkpoint"])


def compare_dfs(df_real, df_fake, scatterplot=dict(x=[], y=[], samples=400),
                table_groupby=[], histogram=dict(variables=[], nrow=1, ncol=1),
                figsize=3):
    """
    Diagnostic function for comparing real and generated data from WGAN models.
    Prints out comparison of means, comparisons of standard deviations, and histograms
    and scatterplots.

    Parameters
    ----------
    df_real: pandas.DataFrame
        real data
    df_fake: pandas.DataFrame
        data produced by generator
    scatterplot: dict
        Contains specifications for plotting scatterplots of variables in real and fake data
    table_groupby: list
        List of variables to group mean and standard deviation table by
    histogram: dict
        Contains specifications for plotting histograms comparing marginal densities
        of real and fake data

    """
    # data prep
    if "source" in list(df_real.columns): df_real = df_real.drop("source", axis=1)
    if "source" in list(df_fake.columns): df_fake = df_fake.drop("source", axis=1)
    df_real.insert(0, "source", "real"), df_fake.insert(0, "source", "fake")
    common_cols = [c for c in df_real.columns if c in df_fake.columns]
    df_joined = pd.concat([df_real[common_cols], df_fake[common_cols]], axis=0, ignore_index=True)
    df_real, df_fake = df_real.drop("source", axis=1), df_fake.drop("source", axis=1)
    common_cols = [c for c in df_real.columns if c in df_fake.columns]
    # mean and std table
    print("-------------comparison of means-------------")
    means = df_joined.groupby(table_groupby + ["source"]).mean().round(2).transpose()
    print(means)
    print("-------------comparison of stds-------------")
    stds = df_joined.groupby(table_groupby + ["source"]).std().round(2).transpose()
    print(stds)
    # covariance matrix comparison
    fig1 = plt.figure(figsize=(figsize * 2, figsize * 1))
    s1 = [fig1.add_subplot(1, 2, i) for i in range(1, 3)]
    s1[0].set_xlabel("real")
    s1[1].set_xlabel("fake")
    s1[0].matshow(df_real[common_cols].corr())
    s1[1].matshow(df_fake[common_cols].corr())
    # histogram marginals
    if histogram and len(histogram["variables"]) > 0:
        fig2, axarr2 = plt.subplots(histogram["nrow"], histogram["ncol"],
                                    figsize=(histogram["nrow"]*figsize, histogram["ncol"]*figsize))
        v = 0
        for i in range(histogram["nrow"]):
            for j in range(histogram["ncol"]):
                plot_var, v = histogram["variables"][v], v+1
                axarr2[i][j].hist([df_real[plot_var], df_fake[plot_var]], bins=8, density=1,
                                  histtype='bar', label=["real", "fake"], color=["blue", "red"])
                axarr2[i][j].legend(prop={"size": 10})
                axarr2[i][j].set_title(plot_var)
        fig2.show()
    # scatterplot grid
    if scatterplot and len(scatterplot["x"]) * len(scatterplot["y"]) > 0:
        df_real_sample = df_real.sample(scatterplot["samples"])
        df_fake_sample = df_fake.sample(scatterplot["samples"])
        x_vars, y_vars = scatterplot["x"], scatterplot["y"]
        fig3 = plt.figure(figsize=(len(x_vars) * figsize, len(y_vars) * figsize))
        s3 = [fig3.add_subplot(len(y_vars), len(x_vars), i + 1) for i in range(len(x_vars) * len(y_vars))]
        for y in y_vars:
            for x in x_vars:
                s = s3.pop(0)
                x_real, y_real = df_real_sample[x].to_numpy(),  df_real_sample[y].to_numpy()
                x_fake, y_fake = df_fake_sample[x].to_numpy(), df_fake_sample[y].to_numpy()
                s.scatter(x_real, y_real, color="blue")
                s.scatter(x_fake, y_fake, color="red")
                s.set_ylabel(y)
                s.set_xlabel(x)
        fig3.show()


if __name__ == "__main__":
    file = "data/original_data/cps_merged.feather"
    df = pd.read_feather(file)

    continuous_vars = ["age", "education", "re74", "re75", "re78"]
    continuous_lower_bounds = {"re74": 0, "re75": 0, "re78": 0}
    categorical_vars = ["black", "hispanic", "married", "nodegree"]
    context_vars = ["t"]

    data_wrapper = DataWrapper(df, continuous_vars, categorical_vars, context_vars, continuous_lower_bounds)
    x, context = data_wrapper.preprocess(df)

    specifications = Specifications(data_wrapper)

    generator = Generator(specifications)
    critic = Critic(specifications)

    train(generator, critic, x, context, specifications)

    df = data_wrapper.apply_critic(critic, df, colname="critic")
    df_fake = data_wrapper.apply_generator(generator, df.sample(int(1e5), replace=True))
    df_fake = data_wrapper.apply_critic(critic, df_fake, colname="critic")

    compare_dfs(df, df_fake,
                scatterplot=dict(x=["t", "age", "education", "re74", "married"],
                                 y=["re78", "critic"], samples=400),
                table_groupby=["t"],
                histogram=dict(variables=['black', 'hispanic', 'married', 'nodegree',
                                          're74', 're75', 're78', 'education', 'age'],
                               nrow=3, ncol=3),
                figsize=3)
