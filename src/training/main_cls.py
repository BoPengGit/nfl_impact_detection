import gc
import torch
import numpy as np
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import GroupKFold

from training.train import fit_cls
from training.predict import predict_cls
from data.dataset import NFLDatasetCls
from data.transforms import get_transfos_cls
from model_zoo.models_cls import get_model_cls
from utils.torch import seed_everything, count_parameters, save_model_weights


def train_cls(config, df_train, df_val, fold, log_folder=None):
    """
    Trains and validate a model.

    Args:
        config (Config): Parameters.
        df_train (pandas dataframe): Training metadata.
        df_val (pandas dataframe): Validation metadata.
        fold (int): Selected fold.
        log_folder (None or str, optional): Folder to logs results to. Defaults to None.

    Returns:
        np array: Validation predictions.
        pandas dataframe: Training history.
    """

    seed_everything(config.seed)

    model = get_model_cls(
        config.name,
        num_classes=config.num_classes,
        num_classes_aux=config.num_classes_aux,
    ).to(config.device)

    model.zero_grad()

    train_dataset = NFLDatasetCls(
        df_train.copy(),
        transforms=get_transfos_cls(train=True),
        root=config.img_path,
        target_name=config.target_name,
    )

    val_dataset = NFLDatasetCls(
        df_val.copy(),
        transforms=get_transfos_cls(train=False),
        root=config.img_path,
        target_name=config.target_name,
    )

    n_parameters = count_parameters(model)

    print(f"    -> {len(train_dataset)} training images")
    print(f"    -> {len(val_dataset)} validation images")
    print(f"    -> {n_parameters} trainable parameters\n")

    pred_val, pred_val_aux = fit_cls(
        model,
        train_dataset,
        val_dataset,
        optimizer_name=config.optimizer,
        samples_per_player=config.samples_per_player,
        epochs=config.epochs,
        batch_size=config.batch_size,
        val_bs=config.val_bs,
        lr=config.lr,
        acc_steps=config.acc_steps,
        warmup_prop=config.warmup_prop,
        swa_first_epoch=config.swa_first_epoch,
        num_classes_aux=config.num_classes_aux,
        aux_mode=config.aux_mode,
        verbose=config.verbose,
        first_epoch_eval=config.first_epoch_eval,
        device=config.device,
    )

    if config.save_weights and log_folder is not None:
        save_model_weights(
            model,
            f"{config.name}_{fold}.pt",
            cp_folder=log_folder,
        )

    torch.cuda.empty_cache()
    return pred_val, pred_val_aux, model


def k_fold_cls(config, df, log_folder=None):
    """
    Performs a video grouped k-fold cross validation for the classification task.

    Args:
        config (Config): Parameters.
        df (pandas dataframe): Metadata.
        log_folder (None or str, optional): Folder to logs results to. Defaults to None.
    """

    if "val_idx" not in df.columns:
        gkf = GroupKFold(n_splits=config.k)
        splits = list(gkf.split(X=df, y=df, groups=df["gameKey"]))
    else:
        idx = np.arange(len(df))
        splits = [
            (idx[df["val_idx"] != k], idx[df["val_idx"] == k]) for k in range(config.k)
        ]

    pred_oof = np.zeros(len(df))
    pred_oof_aux = np.zeros((len(df), config.num_classes_aux))

    for i, (train_idx, val_idx) in enumerate(splits):
        if i in config.selected_folds:
            print(f"\n-------------   Fold {i + 1} / {config.k}  -------------\n")

            df_train = df.iloc[train_idx].copy()
            df_train = df_train[df_train["frame_has_impact"] == 1]

            df_val = df.iloc[val_idx].copy()
            df_val = df_val[df_val["frame_has_impact"] == 1]

            df_val_full = df.iloc[val_idx].copy()

            pred_val, pred_val_aux, model = train_cls(
                config, df_train, df_val, i, log_folder=log_folder
            )

            print('\n - Predicting on full data.')

            pred_val_full, pred_val_aux_full = predict_cls(
                model,
                df_val_full,
                batch_size=config.val_bs,
                img_path=config.img_path,
                num_classes_aux=config.num_classes_aux,
                aux_mode=config.aux_mode,
            )
            pred_oof_aux[val_idx] = pred_val_aux_full
            pred_oof[val_idx] = pred_val_full

            if log_folder is not None:
                np.save(log_folder + f"preds_{i}.npy", pred_val)
                np.save(log_folder + f"preds_aux_{i}.npy", pred_val_aux)
                np.save(log_folder + f"preds_full_{i}.npy", pred_val_full)
                np.save(log_folder + f"preds_aux_full_{i}.npy", pred_val_aux_full)
                np.save(log_folder + f"val_idx_{i}.npy", val_idx)

            del model, df_val, df_val_full, df_train, pred_val_full, pred_val_aux_full
            gc.collect()
            torch.cuda.empty_cache()

    if log_folder is not None:
        np.save(log_folder + "pred_oof.npy", pred_oof)
        np.save(log_folder + "pred_oof_aux.npy", pred_oof_aux)

    auc = roc_auc_score(df[config.target_name], pred_oof)
    print(f"\n  -> CV auc : {auc:.4f}")

    return pred_oof, pred_oof_aux
