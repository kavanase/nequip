from typing import Optional, Tuple, List
import sys
import math
import os
import shutil
import argparse
import logging
import textwrap
from pathlib import Path
import contextlib
from tqdm.auto import tqdm

import ase.io

import torch
import torch.distributed as dist

from nequip.data import (
    AtomicData,
    Collater,
    register_fields,
    _register_field_prefix,
)
from nequip.scripts.deploy import load_deployed_model, R_MAX_KEY, TYPE_NAMES_KEY
from nequip.scripts._logger import set_up_script_logger
from nequip.scripts.train import default_config, check_code_version, _load_datasets
from nequip.utils._global_options import _set_global_options, _init_distributed
from nequip.train import Trainer, Loss, Metrics
from nequip.utils import load_file, instantiate, Config

ORIGINAL_DATASET_PREFIX: str = "original_dataset_"
ORIGINAL_DATASET_INDEX_KEY: str = ORIGINAL_DATASET_PREFIX + "index"
register_fields(graph_fields=[ORIGINAL_DATASET_INDEX_KEY])


def _load_deployed_or_traindir(
    path: Path, device, freeze: bool = True
) -> Tuple[torch.nn.Module, bool, float, List[str]]:
    loaded_deployed_model: bool = False
    model_r_max = None
    type_names = None
    try:
        model, metadata = load_deployed_model(
            path,
            device=device,
            set_global_options=True,  # don't warn that setting
            freeze=freeze,
        )
        # the global settings for a deployed model are set by
        # set_global_options in the call to load_deployed_model
        # above
        model_r_max = float(metadata[R_MAX_KEY])
        type_names = metadata[TYPE_NAMES_KEY].split(" ")
        loaded_deployed_model = True
    except ValueError:  # its not a deployed model
        loaded_deployed_model = False
    # we don't do this in the `except:` block to avoid "during handing of this exception another exception"
    # chains if there is an issue loading the training session model. This makes the error messages more
    # comprehensible:
    if not loaded_deployed_model:
        # Use the model config, regardless of dataset config
        global_config = path.parent / "config.yaml"
        global_config = Config.from_file(str(global_config), defaults=default_config)
        _set_global_options(global_config)
        check_code_version(global_config)
        del global_config

        # load a training session model
        model, model_config = Trainer.load_model_from_training_session(
            traindir=path.parent, model_name=path.name
        )
        model = model.to(device)
        model_r_max = model_config["r_max"]
        type_names = model_config["type_names"]
    model.eval()
    return model, loaded_deployed_model, model_r_max, type_names


def main(args=None, running_as_script: bool = True):
    # in results dir, do: nequip-deploy build --train-dir . deployed.pth
    parser = argparse.ArgumentParser(
        description=textwrap.dedent(
            """Compute the error of a model on a test set using various metrics.

            The model, metrics, dataset, etc. can specified in individual YAML config files, or a training session can be indicated with `--train-dir`.
            In order of priority, the global settings (dtype, TensorFloat32, etc.) are taken from:
              (1) the model config (for a training session),
              (2) the dataset config (for a deployed model),
              or (3) the defaults.

            Prints only the final result in `name = num` format to stdout; all other information is `logging.debug`ed to stderr.

            Please note that results of CUDA models are rarely exactly reproducible, and that even CPU models can be nondeterministic. This is very rarely important in practice, but can be unintuitive.
            """
        )
    )
    parser.add_argument(
        "--train-dir",
        help="Path to a working directory from a training session.",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--model",
        help="A deployed or pickled NequIP model to load. If omitted, defaults to `best_model.pth` in `train_dir`.",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--dataset-config",
        help="A YAML config file specifying the dataset to load test data from. If omitted, `config.yaml` in `train_dir` will be used",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--metrics-config",
        help="A YAML config file specifying the metrics to compute. If omitted, `config.yaml` in `train_dir` will be used. If the config does not specify `metrics_components`, the default is to logging.debug MAEs and RMSEs for all fields given in the loss function. If the literal string `None`, no metrics will be computed.",
        type=str,
        default=None,
    )
    parser.add_argument(
        "--test-indexes",
        help="Path to a file containing the indexes in the dataset that make up the test set. If omitted, all data frames *not* used as training or validation data in the training session `train_dir` will be used. PyTorch, YAML, and JSON formats containing a list of integers are supported.",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--batch-size",
        help="Batch size to use. Larger is usually faster on GPU. If you run out of memory, lower this. You can also try to raise this for faster evaluation. Default: 50.",
        type=int,
        default=50,
    )
    parser.add_argument(
        "--repeat",
        help=(
            "Number of times to repeat evaluating the test dataset. "
            "This can help compensate for CUDA nondeterminism, or can be used to evaluate error on models whose inference passes are intentionally nondeterministic. "
            "Note that `--repeat`ed passes over the dataset will also be `--output`ed if an `--output` is specified."
        ),
        type=int,
        default=1,
    )
    parser.add_argument(
        "--use-deterministic-algorithms",
        help="Try to have PyTorch use deterministic algorithms. Will probably fail on GPU/CUDA.",
        type=bool,
        default=False,
    )
    parser.add_argument(
        "--device",
        help="Device to run the model on. If not provided, defaults to CUDA if available and CPU otherwise.",
        type=str,
        default=None,
    )
    parser.add_argument(
        "--distributed",
        help="Whether to distribute with `torch.distributed`.",
        const="nccl" if torch.cuda.is_available() else "gloo",
        type=str,
        nargs="?",
    )
    parser.add_argument(
        "--output",
        help="ExtXYZ (.xyz) file to write out the test set and model predictions to.",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--output-fields",
        help="Extra fields (names comma separated with no spaces) to write to the `--output`.",
        type=str,
        default="",
    )
    parser.add_argument(
        "--output-fields-from-original-dataset",
        help="Extra fields from the ORIGINAL REFERENCE DATASET (names comma separated with no spaces) to write to the `--output` with the added prefix `original_dataset_*`",
        type=str,
        default="",
    )
    parser.add_argument(
        "--log",
        help="log file to store all the metrics and screen logging.debug",
        type=Path,
        default=None,
    )
    # Something has to be provided
    # See https://stackoverflow.com/questions/22368458/how-to-make-argparse-logging.debug-usage-when-no-option-is-given-to-the-code
    if len(sys.argv) == 1:
        parser.print_help()
        parser.exit()
    # Parse the args
    args = parser.parse_args(args=args)

    # Do the defaults:
    dataset_is_from_training: bool = False
    if args.train_dir:
        if args.dataset_config is None:
            args.dataset_config = args.train_dir / "config.yaml"
            dataset_is_from_training = True
        if args.metrics_config is None:
            args.metrics_config = args.train_dir / "config.yaml"
        if args.model is None:
            args.model = args.train_dir / "best_model.pth"
        if args.test_indexes is None:
            # Find the remaining indexes that arent train or val
            trainer = torch.load(
                str(args.train_dir / "trainer.pth"), map_location="cpu"
            )
            train_idcs = set(trainer["train_idcs"].tolist())
            val_idcs = set(trainer["val_idcs"].tolist())
        else:
            train_idcs = val_idcs = None
    # update
    if args.metrics_config == "None":
        args.metrics_config = None
    elif args.metrics_config is not None:
        args.metrics_config = Path(args.metrics_config)
    do_metrics = args.metrics_config is not None
    # validate
    if args.dataset_config is None:
        raise ValueError("--dataset-config or --train-dir must be provided")
    if args.metrics_config is None and args.output is None:
        raise ValueError(
            "Nothing to do! Must provide at least one of --metrics-config, --train-dir (to use training config for metrics), or --output"
        )
    if args.model is None:
        raise ValueError("--model or --train-dir must be provided")
    output_type: Optional[str] = None
    if args.output is not None:
        if args.output.suffix != ".xyz":
            raise ValueError("Only .xyz format for `--output` is supported.")
        args.output_fields_from_original_dataset = [
            e for e in args.output_fields_from_original_dataset.split(",") if e != ""
        ]
        args.output_fields = [e for e in args.output_fields.split(",") if e != ""]
        ase_all_fields = (
            args.output_fields
            + [
                ORIGINAL_DATASET_PREFIX + e
                for e in args.output_fields_from_original_dataset
            ]
            + [ORIGINAL_DATASET_INDEX_KEY]
        )
        if len(args.output_fields_from_original_dataset) > 0:
            _register_field_prefix(ORIGINAL_DATASET_PREFIX)
        output_type = "xyz"
    else:
        assert args.output_fields == ""
        args.output_fields = []

    if running_as_script:
        set_up_script_logger(args.log)
    logger = logging.getLogger("nequip-evaluate")
    logger.setLevel(logging.INFO)

    # Handle devices and setup
    if args.device is None:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    else:
        device = torch.device(args.device)

    logger.info(f"Using device: {device}")
    if device.type == "cuda":
        logger.info(
            "Please note that _all_ machine learning models running on CUDA hardware are generally somewhat nondeterministic and that this can manifest in small, generally unimportant variation in the final test errors.",
        )

    if args.use_deterministic_algorithms:
        logger.info(
            "Telling PyTorch to try to use deterministic algorithms... please note that this will likely error on CUDA/GPU"
        )
        torch.use_deterministic_algorithms(True)

    _init_distributed(args.distributed)

    # Load model:
    logger.info("Loading model... ")
    model, loaded_deployed_model, model_r_max, _ = _load_deployed_or_traindir(
        args.model, device=device
    )
    logger.info(f"    loaded{' deployed' if loaded_deployed_model else ''} model")

    # Load a config file
    logger.info(
        f"Loading {'original ' if dataset_is_from_training else ''}dataset...",
    )
    dataset_config = Config.from_file(
        str(args.dataset_config), defaults={"r_max": model_r_max}
    )
    if dataset_config["r_max"] != model_r_max:
        raise RuntimeError(
            f"Dataset config has r_max={dataset_config['r_max']}, but model has r_max={model_r_max}!"
        )

    dataset_is_validation: bool = False
    # look for validation and only fall back to `dataset` prefix
    # have to tell the loading function whether to use distributed
    dataset_config.distributed = args.distributed
    # this function syncs distributed if it is enabled
    datasets = _load_datasets(
        dataset_config,
        prefixes=["validation_dataset", "dataset"],
        stop_on_first_found=True,
    )
    if datasets["validation_dataset"] is not None:
        dataset = datasets["validation_dataset"]
        dataset_is_validation = True
    else:
        dataset = datasets["dataset"]
    del datasets
    assert dataset is not None

    logger.info(
        f"Loaded {'validation_' if dataset_is_validation else ''}dataset specified in {args.dataset_config.name}.",
    )

    c = Collater.for_dataset(dataset, exclude_keys=[])

    # Determine the test set
    # this makes no sense if a dataset is given seperately
    if (
        args.test_indexes is None
        and dataset_is_from_training
        and train_idcs is not None
    ):
        # we know the train and val, get the rest
        all_idcs = set(range(len(dataset)))
        # set operations
        if dataset_is_validation:
            test_idcs = list(all_idcs - val_idcs)
            logger.info(
                f"Using origial validation dataset ({len(dataset)} frames) minus validation set frames ({len(val_idcs)} frames), yielding a test set size of {len(test_idcs)} frames.",
            )
        else:
            test_idcs = list(all_idcs - train_idcs - val_idcs)
            assert set(test_idcs).isdisjoint(train_idcs)
            logger.info(
                f"Using origial training dataset ({len(dataset)} frames) minus training ({len(train_idcs)} frames) and validation frames ({len(val_idcs)} frames), yielding a test set size of {len(test_idcs)} frames.",
            )
        # No matter what it should be disjoint from validation:
        assert set(test_idcs).isdisjoint(val_idcs)
        if not do_metrics:
            logger.info(
                "WARNING: using the automatic test set ^^^ but not computing metrics, is this really what you wanted to do?",
            )
    elif args.test_indexes is None:
        # Default to all frames
        test_idcs = torch.arange(dataset.len())
        logger.info(
            f"Using all frames from the specified test dataset, yielding a test set size of {len(test_idcs)} frames.",
        )
    else:
        # load from file
        test_idcs = load_file(
            supported_formats=dict(
                torch=["pt", "pth"], yaml=["yaml", "yml"], json=["json"]
            ),
            filename=str(args.test_indexes),
        )
        logger.info(
            f"Using provided test set indexes, yielding a test set size of {len(test_idcs)} frames.",
        )
    test_idcs = torch.as_tensor(test_idcs, dtype=torch.long)
    test_idcs = test_idcs.tile((args.repeat,))

    # Figure out what metrics we're actually computing
    if do_metrics:
        metrics_config = Config.from_file(str(args.metrics_config))
        metrics_components = metrics_config.get("metrics_components", None)
        # See trainer.py: init() and init_metrics()
        # Default to loss functions if no metrics specified:
        if metrics_components is None:
            loss, _ = instantiate(
                builder=Loss,
                prefix="loss",
                positional_args=dict(coeffs=metrics_config.loss_coeffs),
                all_args=metrics_config,
            )
            metrics_components = []
            for key, func in loss.funcs.items():
                params = {
                    "PerSpecies": type(func).__name__.startswith("PerSpecies"),
                }
                metrics_components.append((key, "mae", params))
                metrics_components.append((key, "rmse", params))

        metrics, _ = instantiate(
            builder=Metrics,
            prefix="metrics",
            positional_args=dict(components=metrics_components),
            all_args=metrics_config,
        )
        metrics.to(device=device)

    batch_i: int = 0
    batch_size: int = args.batch_size

    is_rank_zero: bool = True
    if args.distributed:
        is_rank_zero = dist.get_rank() == 0
        # divide the frames between ranks
        n_per_rank = int(math.ceil(len(test_idcs) / dist.get_world_size()))
        test_idcs = test_idcs[
            dist.get_rank() * n_per_rank : (dist.get_rank() + 1) * n_per_rank
        ]

    logger.info("Starting...")
    with contextlib.ExitStack() as context_stack:
        if is_rank_zero:
            # only do output on rank zero
            # "None" checks if in a TTY and disables if not
            prog = context_stack.enter_context(tqdm(total=len(test_idcs), disable=None))
            if do_metrics:
                display_bar = context_stack.enter_context(
                    tqdm(
                        bar_format=(
                            ""
                            if prog.disable  # prog.ncols doesn't exist if disabled
                            else ("{desc:." + str(prog.ncols) + "}")
                        ),
                        disable=None,
                    )
                )

        if output_type is not None:
            if args.distributed:
                # give each rank its own output and merge later
                # we do NOT guerantee that the final XYZ is in any order
                # just that we include the indexes into the original dataset
                # so this is OK
                outfile = args.output.parent / (
                    args.output.stem + f"-rank{dist.get_rank()}.xyz"
                )
            else:
                outfile = args.output
            output = context_stack.enter_context(open(outfile, "w"))
        else:
            output = None

        while True:
            this_batch_test_indexes = test_idcs[
                batch_i * batch_size : (batch_i + 1) * batch_size
            ]
            datas = [dataset[int(idex)] for idex in this_batch_test_indexes]
            if len(datas) == 0:
                break
            batch = c.collate(datas)
            batch = batch.to(device)
            out = model(AtomicData.to_AtomicDataDict(batch))

            with torch.no_grad():
                # Write output
                if output_type == "xyz":
                    output_out = out.copy()
                    # add test frame to the output:
                    output_out[ORIGINAL_DATASET_INDEX_KEY] = torch.LongTensor(
                        this_batch_test_indexes
                    )
                    for field in args.output_fields_from_original_dataset:
                        # batch is from the original dataset
                        output_out[ORIGINAL_DATASET_PREFIX + field] = batch[field]
                    # append to the file
                    ase.io.write(
                        output,
                        AtomicData.from_AtomicDataDict(output_out)
                        .to(device="cpu")
                        .to_ase(
                            type_mapper=dataset.type_mapper,
                            extra_fields=ase_all_fields,
                        ),
                        format="extxyz",
                        append=True,
                    )
                    del output_out

                # Accumulate metrics
                if do_metrics:
                    metrics(out, batch)
                    if args.distributed:
                        # sync metrics across ranks
                        metrics.gather()
                    if is_rank_zero:
                        display_bar.set_description_str(
                            " | ".join(
                                f"{k} = {v:4.4f}"
                                for k, v in metrics.flatten_metrics(
                                    metrics.current_result()
                                )[0].items()
                            )
                        )

            batch_i += 1
            if is_rank_zero:
                prog.update(batch.num_graphs)

        if is_rank_zero:
            prog.close()
            if do_metrics:
                display_bar.close()

    if args.distributed and output_type is not None:
        os.sync()

        if is_rank_zero:
            logger.info("Merging output files...")
            output_files = [
                args.output.parent / (args.output.stem + f"-rank{rank}.xyz")
                for rank in range(dist.get_world_size())
            ]
            with open(args.output, "wb") as wfd:
                for f in output_files:
                    with open(f, "rb") as fd:
                        shutil.copyfileobj(fd, wfd)
                        wfd.write(b"\n")
            os.sync()
            # delete old ones
            for f in output_files:
                f.unlink()

    if is_rank_zero and do_metrics:
        logger.info("\n--- Final result: ---")
        logger.critical(
            "\n".join(
                f"{k:>20s} = {v:< 20f}"
                for k, v in metrics.flatten_metrics(
                    metrics.current_result(),
                    type_names=dataset.type_mapper.type_names,
                )[0].items()
            )
        )


if __name__ == "__main__":
    main(running_as_script=True)
