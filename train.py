# train.py

import os
import sys
from typing import Dict, Any, Optional

import pytorch_lightning as pl
import torch
from loguru import logger
from pytorch_lightning.callbacks import (
    ModelCheckpoint,
    EarlyStopping,
    LearningRateMonitor,
)
from pytorch_lightning.loggers import TensorBoardLogger

from core.data.datamodule import SlimPajamaDataModule
from core.models.uncertainty.uncertainty import (
    UncertainTransformerLMHeadModel,
    UncertainTransformerConfig,
)
from core.utils.tokenizer import Tokenizer

# Configure Loguru
log_level = os.environ.get("LOG_LEVEL", "INFO")
logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    level=log_level,
)

# Enable tensor core support
torch.set_float32_matmul_precision("high")


class UncertainTransformerLightningModule(pl.LightningModule):
    def __init__(self, hparams: Dict[str, Any]):
        super().__init__()
        self.save_hyperparameters(hparams)

        config = UncertainTransformerConfig(
            vocab_size=hparams["vocab_size"],
            d_model=hparams["d_model"],
            n_heads=hparams["n_heads"],
            d_ff=hparams["d_ff"],
            n_layers=hparams["n_layers"],
            dropout=hparams["dropout"],
            max_position_embeddings=hparams["max_length"],
            pad_token_id=hparams["pad_token_id"],
            use_mamba=hparams["use_mamba"],
            d_state=hparams["d_state"],
            d_conv=hparams["d_conv"],
            expand_factor=hparams["expand_factor"],
            dt_rank=hparams["dt_rank"],
            dt_min=hparams["dt_min"],
            dt_max=hparams["dt_max"],
            sliding_window_size=hparams["sliding_window_size"],
        )

        self.model = UncertainTransformerLMHeadModel(config)
        self.tokenizer = None

    def forward(self, input_ids, attention_mask=None, labels=None):
        return self.model(input_ids, attention_mask=attention_mask, labels=labels)

    def training_step(
            self, batch: Dict[str, torch.Tensor], batch_idx: int
    ) -> Optional[torch.Tensor]:
        outputs = self(**batch)
        loss = outputs.loss

        if torch.isnan(loss) or torch.isinf(loss):
            self.log(
                "nan_loss",
                1.0,
                on_step=True,
                on_epoch=False,
                prog_bar=True,
                logger=True,
            )
            return None

        self.log(
            "train_loss",
            loss,
            on_step=True,
            on_epoch=True,
            prog_bar=True,
            logger=True,
            sync_dist=True,
        )
        return loss.mean()  # Add .mean() to the loss

    def validation_step(
        self, batch: Dict[str, torch.Tensor], batch_idx: int
    ) -> Dict[str, torch.Tensor]:
        outputs, uncertainty = self(**batch)
        loss = outputs.loss

        self.log(
            "val_loss", loss, on_epoch=True, prog_bar=True, logger=True, sync_dist=True
        )

        perplexity = torch.exp(loss)

        self.log(
            "val_perplexity",
            perplexity,
            on_epoch=True,
            prog_bar=True,
            logger=True,
            sync_dist=True,
        )

        # Log mean uncertainty
        mean_uncertainty = uncertainty.mean()
        self.log(
            "val_mean_uncertainty",
            mean_uncertainty,
            on_epoch=True,
            prog_bar=True,
            logger=True,
            sync_dist=True,
        )

        # Sample Generation for Logging
        if batch_idx == 0:
            sample_input_ids = batch["input_ids"][:1]
            generated = self.model.generate(sample_input_ids, max_new_tokens=50)
            generated_text = self.tokenizer.decode(
                generated[0], skip_special_tokens=True
            )
            self.logger.experiment.add_text(
                "generated_text", generated_text, self.current_epoch
            )

        return {
            "val_loss": loss,
            "val_perplexity": perplexity,
            "val_uncertainty": mean_uncertainty,
        }

    def configure_optimizers(self):
        optimizer = torch.optim.AdamW(
            self.parameters(),
            lr=self.hparams["learning_rate"],
            weight_decay=self.hparams["weight_decay"],
        )
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
            optimizer,
            T_max=self.trainer.estimated_stepping_batches,
            eta_min=self.hparams["learning_rate"] / 10,
        )
        return {
            "optimizer": optimizer,
            "lr_scheduler": {
                "scheduler": scheduler,
                "interval": "step",
                "frequency": 1,
            },
        }


def main():
    logger.info("Starting main function...")

    hparams = {
        "vocab_size": 50257,
        "d_model": 256,
        "n_heads": 8,
        "d_ff": 256,
        "n_layers": 2,
        "dropout": 0.1,
        "learning_rate": 3e-4,
        "weight_decay": 0.01,
        "max_length": 1024,
        "batch_size": 8,
        "accumulate_grad_batches": 32,
        "max_epochs": 10,
        "pad_token_id": 50256,
        "use_mamba": True,
        "d_state": 8,
        "d_conv": 2,
        "expand_factor": 1.1,
        "dt_rank": 8,
        "dt_min": 0.001,
        "dt_max": 0.1,
        "sliding_window_size": 512,
    }

    logger.info("Initializing model...")
    model = UncertainTransformerLightningModule(hparams)

    logger.info("Loading tokenizer...")
    tokenizer = Tokenizer()
    model.tokenizer = tokenizer

    logger.info("Initializing DataModule...")
    datamodule = SlimPajamaDataModule(
        tokenizer=tokenizer,
        max_length=hparams["max_length"],
        batch_size=hparams["batch_size"],
        streaming=True,
    )

    logger.info("Setting up callbacks...")
    checkpoint_callback = ModelCheckpoint(
        dirpath="checkpoints",
        filename="uncertain-transformer-{epoch:02d}-{val_loss:.2f}",
        save_top_k=3,
        monitor="val_loss",
        mode="min",
    )
    early_stop_callback = EarlyStopping(monitor="val_loss", patience=3, mode="min")
    lr_monitor = LearningRateMonitor(logging_interval="step")

    logger.info("Setting up TensorBoard logger...")
    tb_logger = TensorBoardLogger("logs", name="uncertain-transformer")

    logger.info("Setting up trainer...")
    trainer = pl.Trainer(
        max_epochs=hparams["max_epochs"],
        callbacks=[checkpoint_callback, early_stop_callback, lr_monitor],
        logger=tb_logger,
        precision="16-mixed" if torch.cpu.is_available() else 32,
        accumulate_grad_batches=hparams["accumulate_grad_batches"],
        gradient_clip_val=1.0,
        log_every_n_steps=10,
        val_check_interval=0.25,
    )

    logger.info("Starting training...")
    trainer.fit(model, datamodule=datamodule)

    logger.info("Training completed.")


if __name__ == "__main__":
    main()
