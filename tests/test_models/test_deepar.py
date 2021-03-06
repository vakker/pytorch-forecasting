import pickle
import shutil

import pytest
import pytorch_lightning as pl
from pytorch_lightning.callbacks import EarlyStopping, ModelCheckpoint
from pytorch_lightning.loggers import TensorBoardLogger
from test_models.conftest import make_dataloaders
from torch import nn

from pytorch_forecasting.data.encoders import GroupNormalizer
from pytorch_forecasting.metrics import (
    BetaDistributionLoss,
    LogNormalDistributionLoss,
    NegativeBinomialDistributionLoss,
)
from pytorch_forecasting.models import DeepAR


def _integration(
    data_with_covariates, tmp_path, gpus, cell_type="LSTM", data_loader_kwargs={}, clip_target: bool = False, **kwargs
):
    if clip_target:
        data_with_covariates["target"] = data_with_covariates["volume"].clip(1e-3, 1.0)
    else:
        data_with_covariates["target"] = data_with_covariates["volume"]
    data_loader_default_kwargs = dict(
        target="target",
        time_varying_known_reals=["price_actual"],
        time_varying_unknown_reals=["target"],
        static_categoricals=["agency"],
        add_relative_time_idx=True,
    )
    data_loader_default_kwargs.update(data_loader_kwargs)
    dataloaders_with_covariates = make_dataloaders(data_with_covariates, **data_loader_default_kwargs)
    train_dataloader = dataloaders_with_covariates["train"]
    val_dataloader = dataloaders_with_covariates["val"]
    early_stop_callback = EarlyStopping(monitor="val_loss", min_delta=1e-4, patience=1, verbose=False, mode="min")

    logger = TensorBoardLogger(tmp_path)
    trainer = pl.Trainer(
        max_epochs=3,
        gpus=gpus,
        weights_summary="top",
        gradient_clip_val=0.1,
        callbacks=[early_stop_callback],
        checkpoint_callback=True,
        default_root_dir=tmp_path,
        limit_train_batches=2,
        limit_val_batches=2,
        logger=logger,
    )

    net = DeepAR.from_dataset(
        train_dataloader.dataset,
        cell_type=cell_type,
        learning_rate=0.15,
        log_gradient_flow=True,
        log_interval=1000,
        n_plotting_samples=100,
        **kwargs
    )
    net.size()
    try:
        trainer.fit(
            net,
            train_dataloader=train_dataloader,
            val_dataloaders=val_dataloader,
        )
        # check loading
        net = DeepAR.load_from_checkpoint(trainer.checkpoint_callback.best_model_path)

        # check prediction
        net.predict(val_dataloader, fast_dev_run=True, return_index=True, return_decoder_lengths=True)
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)

    net.predict(val_dataloader, fast_dev_run=True, return_index=True, return_decoder_lengths=True)


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"cell_type": "GRU"},
        dict(
            loss=LogNormalDistributionLoss(),
            clip_target=True,
            data_loader_kwargs=dict(target_normalizer=GroupNormalizer(groups=["agency", "sku"], transformation="log")),
        ),
        dict(
            loss=NegativeBinomialDistributionLoss(),
            clip_target=False,
            data_loader_kwargs=dict(target_normalizer=GroupNormalizer(groups=["agency", "sku"], center=False)),
        ),
        dict(
            loss=BetaDistributionLoss(),
            clip_target=True,
            data_loader_kwargs=dict(
                target_normalizer=GroupNormalizer(groups=["agency", "sku"], transformation="logit")
            ),
        ),
        dict(
            data_loader_kwargs=dict(
                lags={"volume": [2, 5]}, target="volume", time_varying_unknown_reals=["volume"], min_encoder_length=10
            )
        ),
        dict(
            data_loader_kwargs=dict(
                time_varying_unknown_reals=["volume", "discount"],
                target=["volume", "discount"],
                lags={"volume": [2], "discount": [2]},
            )
        ),
    ],
)
def test_integration(data_with_covariates, tmp_path, gpus, kwargs):
    if "loss" in kwargs and isinstance(kwargs["loss"], NegativeBinomialDistributionLoss):
        data_with_covariates = data_with_covariates.assign(volume=lambda x: x.volume.round())
    _integration(data_with_covariates, tmp_path, gpus, **kwargs)


@pytest.fixture
def model(dataloaders_with_covariates):
    dataset = dataloaders_with_covariates["train"].dataset
    net = DeepAR.from_dataset(
        dataset,
        learning_rate=0.15,
        log_gradient_flow=True,
        log_interval=1000,
    )
    return net


def test_pickle(model):
    pkl = pickle.dumps(model)
    pickle.loads(pkl)
