import torch
import pytorch_lightning as pl
from pytorch_forecasting import TemporalFusionTransformer, TimeSeriesDataSet
from pytorch_forecasting.metrics import QuantileLoss

from .loss import AdaptivePinballLoss


class M4FullModel(pl.LightningModule):
    """
    M4 확장 모델 (추론 전용 vendored 버전).

    [M4-Combined] use_garch_sigma=True + vol_group static categorical
                   + ticker_to_vol_group_idx
      → σ 자리 GARCH_Variance + vol_group 별 α/β 적용

    추론 시에는 AdaptivePinballLoss(학습용)는 호출되지 않고 self.tft 의 forward
    /predict 만 사용한다. loss 코드는 from_dataset 구성과 state_dict 호환을 위해
    남겨둔다.
    """

    def __init__(
        self,
        tft: TemporalFusionTransformer,
        adaptive_loss: AdaptivePinballLoss,
        vix_encoder_idx: int,
        sigma_encoder_idx: int,
        learning_rate: float = 0.001,
        vix_mean: float = 0.0,
        vix_std: float = 1.0,
        ticker_to_vol_group_idx: torch.Tensor = None,
    ):
        super().__init__()
        self.tft = tft
        self.adaptive_loss = adaptive_loss
        self.vix_encoder_idx = vix_encoder_idx
        self.sigma_encoder_idx = sigma_encoder_idx
        self.learning_rate = learning_rate
        self.vix_mean = vix_mean
        self.vix_std = vix_std

        # ticker idx → vol_group idx 매핑 buffer (None 이면 empty tensor)
        if ticker_to_vol_group_idx is None:
            ticker_to_vol_group_idx = torch.tensor([], dtype=torch.long)
        self.register_buffer(
            "ticker_to_vol_group_idx", ticker_to_vol_group_idx
        )

    @classmethod
    def from_dataset(
        cls,
        dataset: TimeSeriesDataSet,
        learning_rate: float = 0.001,
        hidden_size: int = 64,
        attention_head_size: int = 4,
        dropout: float = 0.1,
        hidden_continuous_size: int = 32,
        quantiles: list[float] = None,
        vix_threshold: float = 25.0,
        vix_mean: float = 0.0,
        vix_std: float = 1.0,
        sigma_scale: float = 1.0,
        alpha_down: float = 1.0,
        beta_down: float = 1.0,
        alpha_up: float = 1.0,
        beta_up: float = 1.0,
        crossing_weight: float = 0.1,
        use_garch_sigma: bool = True,
        ticker_to_vol_group_idx: torch.Tensor = None,
        group_labels: list[str] = None,
        alpha_down_by_group: dict = None,
        beta_down_by_group: dict = None,
        alpha_up_by_group: dict = None,
        beta_up_by_group: dict = None,
    ) -> "M4FullModel":
        quantiles = quantiles or [0.1, 0.5, 0.9]
        group_labels = group_labels or ["low_vol", "mid_vol", "high_vol"]

        reals: list[str] = dataset.reals
        vix_idx = reals.index("VIX_Close")

        # σ 자리 선택: GARCH_Variance (M4) 우선, 없으면 Realized_Vol_20d (M3 호환)
        if use_garch_sigma and "GARCH_Variance" in reals:
            sigma_idx = reals.index("GARCH_Variance")
        else:
            sigma_idx = reals.index("Realized_Vol_20d")

        tft = TemporalFusionTransformer.from_dataset(
            dataset,
            learning_rate=learning_rate,
            hidden_size=hidden_size,
            attention_head_size=attention_head_size,
            dropout=dropout,
            hidden_continuous_size=hidden_continuous_size,
            output_size=len(quantiles),
            loss=QuantileLoss(quantiles=quantiles),
            log_interval=10,
            reduce_on_plateau_patience=4,
        )

        if ticker_to_vol_group_idx is not None:
            if not isinstance(ticker_to_vol_group_idx, torch.Tensor):
                ticker_to_vol_group_idx = torch.tensor(
                    ticker_to_vol_group_idx, dtype=torch.long
                )
            else:
                ticker_to_vol_group_idx = ticker_to_vol_group_idx.long()

        adaptive_loss = AdaptivePinballLoss(
            quantiles=quantiles,
            vix_threshold=vix_threshold,
            vix_scale=vix_std,
            sigma_scale=sigma_scale,
            alpha_down=alpha_down,
            beta_down=beta_down,
            alpha_up=alpha_up,
            beta_up=beta_up,
            crossing_weight=crossing_weight,
            group_labels=group_labels,
            alpha_down_by_group=alpha_down_by_group,
            beta_down_by_group=beta_down_by_group,
            alpha_up_by_group=alpha_up_by_group,
            beta_up_by_group=beta_up_by_group,
        )

        return cls(
            tft=tft,
            adaptive_loss=adaptive_loss,
            vix_encoder_idx=vix_idx,
            sigma_encoder_idx=sigma_idx,
            learning_rate=learning_rate,
            vix_mean=vix_mean,
            vix_std=vix_std,
            ticker_to_vol_group_idx=ticker_to_vol_group_idx,
        )

    def forward(self, x: dict) -> dict:
        return self.tft(x)

    def predict(self, x: dict) -> torch.Tensor:
        self.eval()
        with torch.no_grad():
            out = self.tft(x)
        return out["prediction"]

    def configure_optimizers(self) -> dict:
        optimizer = torch.optim.Adam(self.parameters(), lr=self.learning_rate)
        scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
            optimizer, mode="min", patience=4, factor=0.5
        )
        return {
            "optimizer": optimizer,
            "lr_scheduler": {"scheduler": scheduler, "monitor": "val_loss"},
        }
