import collections
import os
import pickle
import textwrap
import types

import numpy as np
import openbabel
import torch
from torch_geometric.data.dataloader import DataLoader
from tqdm import tqdm

from delfta.download import get_model_weights
from delfta.net import EGNN
from delfta.net_utils import (
    MODEL_HPARAMS,
    MULTITASK_ENDPOINTS,
    QMUGS_ATOM_DICT,
    DeltaDataset,
)
import openbabel
from delfta.utils import LOGGER, MODEL_PATH, ELEM_TO_ATOMNUM
from delfta.xtb import run_xtb_calc

_ALLTASKS = ["E_form", "E_homo", "E_lumo", "E_gap", "dipole", "charges"]
PLACEHOLDER = float("NaN")


class DelftaCalculator:
    def __init__(
        self,
        tasks="all",
        delta=True,
        force3d=True,
        addh=True,
        xtbopt=False,
        verbose=True,
        progress=True,
    ) -> None:
        """Main calculator class for predicting DFT observables.

        Parameters
        ----------
        tasks : str, optional
            A list of tasks to predict. Available tasks include
            `[E_form, E_homo, E_lumo, E_gap, dipole, charges]`, by default "all".
        delta : bool, optional
            Whether to use delta-learning models, by default True
        force3d : bool, optional
            Whether to assign 3D coordinates to molecules lacking them, by default False
        addh : bool, optional
            Whether to add hydrogens to molecules lacking them, by default False
        xtbopt : bool, optional
            Whether to perform GFN2-xTB geometry optimization, by default False
        verbose : bool, optional
            Enables/disables stdout logger, by default True
        progress : bool, optional
            Enables/disables progress bars in prediction, by default True
        """
        if tasks == "all" or tasks == ["all"]:
            tasks = _ALLTASKS
        self.tasks = tasks
        self.delta = delta
        self.multitasks = [task for task in self.tasks if task in MULTITASK_ENDPOINTS]
        self.force3d = force3d
        self.addh = addh
        self.xtbopt = xtbopt
        self.verbose = verbose
        self.progress = progress

        with open(os.path.join(MODEL_PATH, "norm.pt"), "rb") as handle:
            self.norm = pickle.load(handle)

        self.models = []

        for task in tasks:
            if task in MULTITASK_ENDPOINTS:
                task_name = "multitask"

            elif task == "charges":
                task_name = "charges"

            elif task == "E_form":
                task_name = "single_energy"

            else:
                raise ValueError(f"Task name `{task}` not recognised")

            if self.delta:
                task_name += "_delta"
            else:
                task_name += "_direct"

            self.models.append(task_name)

        self.models = list(set(self.models))

    def _molcheck(self, mol):
        return isinstance(mol, openbabel.pybel.Molecule) and len(mol.atoms) > 0

    def _3dcheck(self, mol):
        """Checks whether `mol` has 3d coordinates assigned. If
        `self.force3d=True` these will be computed for
        those lacking them using the MMFF94 force-field as
        available in pybel.

        Parameters
        ----------
        mol : pybel.Molecule
            An OEChem molecule object

        Returns
        -------
        bool
            `True` if `mol` has a 3d conformation, `False` otherwise.
        """
        if mol.dim != 3:
            if self.force3d:
                mol.make3D()
            return False
        return True

    def _atomtypecheck(self, mol):
        """Checks whether the atom types in `mol` are supported
        by the QMugs database

        Parameters
        ----------
        mol : pybel.Molecule
            An OEChem molecule object

        Returns
        -------
        bool
            `True` if all atoms have valid atom types, `False` otherwise.
        """
        for atom in mol.atoms:
            if atom.atomicnum not in QMUGS_ATOM_DICT:
                return False
        return True

    def _chargecheck(self, mol):
        """Checks whether the overall charge on `mol` is neutral.

        Parameters
        ----------
        mol : pybel.Molecule
            An OEChem molecule object

        Returns
        -------
        bool
            `True` is overall `mol` charge is 0, `False` otherwise.
        """
        if mol.charge != 0:
            return True
        else:
            return False

    def _hydrogencheck(self, mol):
        """Checks whether `mol` has assigned hydrogens. If `self.addh=True`
        these will be added if lacking.

        Parameters
        ----------
        mol : pybel.Molecule
            An OEChem molecule object

        Returns
        -------
        bool
            Whether `mol` has assigned hydrogens.
        """
        nh_mol = sum([True for atom in mol.atoms if atom.atomicnum == 1])
        mol_cpy = mol.clone
        mol_cpy.removeh()
        mol_cpy.addh()
        nh_cpy = sum([True for atom in mol_cpy.atoms if atom.atomicnum == 1])

        if nh_mol == nh_cpy:
            return True
        else:
            return False

    def _preprocess(self, mols):
        """Performs a series of preprocessing checks on a list of molecules `mols`,
        including 3d-conformation existence, validity of atom types, neutral charge
        and hydrogen addition.

        Parameters
        ----------
        mols: [pybel.Molecule]
            A list of OEChem molecule objects

        Returns
        -------
        ([pybel.Molecule], [int])
            A list of processed OEChem molecule objects; and indices of molecules that cannot be processed.

        """
        if len(mols) == 0:
            raise ValueError("No molecules provided.")

        idx_non_valid_mols = []
        idx_non_valid_atypes = []
        idx_charged = []
        idx_no3d = []
        idx_noh = []
        fatal = []

        for idx, mol in enumerate(mols):
            valid_mol = self._molcheck(mol)
            if not valid_mol:
                idx_non_valid_mols.append(idx)
                fatal.append(idx)
                continue  # no need to check further

            is_atype_valid = self._atomtypecheck(mol)
            if not is_atype_valid:
                idx_non_valid_atypes.append(idx)
                fatal.append(idx)
                continue  # no need to check further

            is_charged = self._chargecheck(mol)
            if is_charged:
                idx_charged.append(idx)
                fatal.append(idx)
                continue  # no need to check further

            has_3d = self._3dcheck(mol)
            if not has_3d:
                idx_no3d.append(idx)

            has_h = self._hydrogencheck(mol)
            if not has_h:
                idx_noh.append(idx)

            if not has_3d and not has_h and not self.addh:
                fatal.append(idx)
                # cannot assign 3D geometry without assigning hydrogens

        if idx_non_valid_mols:
            LOGGER.warning(
                f"""Invalid molecules at position(s) {idx_non_valid_mols}."""
            )

        if idx_no3d:
            if self.force3d:
                if self.verbose:
                    LOGGER.info(
                        f"Assigned MMFF94 coordinates and added hydrogens to molecules with idx. {idx_no3d}"
                    )

            else:
                LOGGER.warning(
                    f"""Molecules at position {idx_no3d} have no 3D conformations available. 
                        Either provide a mol with one or re-run calculator with `force3D=True`.
                    """
                )

        if idx_non_valid_atypes:
            LOGGER.warning(
                f"""Found non-supported atomic no. in molecules at position 
                    {idx_non_valid_atypes}. This application currently supports 
                    only the atom types used in the QMugs database, namely 
                    {list(ELEM_TO_ATOMNUM.keys())}.
                """
            )
        if idx_charged:
            LOGGER.warning(
                f"""Found molecules with a non-zero formal charge at 
                    positions {idx_charged}. This application currently does not support
                    prediction for charged molecules.
                """
            )

        if idx_noh:
            if self.addh and not self.force3d:
                LOGGER.info(
                    f"""Added hydrogens for non-protonated molecules at position {idx_noh}"""
                )
            else:
                LOGGER.warning(
                    f"""No hydrogens present for molecules at position {idx_noh}. Please 
                        add them manually or re-run the calculator with argument `addh=True`.
                    """
                )
        good_mols = [mol for idx, mol in enumerate(mols) if idx not in fatal]
        return good_mols, fatal

    def _get_preds(self, loader, model):
        """Returns predictions for the data contained in `loader` of a
        pyTorch `model`.


        Parameters
        ----------
        loader : delfta.net_utils.DeltaDataset
            A `delfta.net_utils.DeltaDataset` instance.
        model : delfta.net.EGNN
            A `delfta.net.EGNN` instance.

        Returns
        -------
        numpy.ndarray
            Model predictions.
        numpy.ndarray
            Graph-specific indexes for node-level predictions.
        """
        y_hats = []
        g_ptrs = []

        if self.progress:
            loader = tqdm(loader)

        with torch.no_grad():
            for batch in loader:
                y_hats.append(model(batch).numpy())
                g_ptrs.append(batch.ptr.numpy())
        return y_hats, g_ptrs

    def _get_xtb_props(self, mols):
        """Runs the GFN2-xTB binary and returns observables

        Parameters
        ----------
        mols : [pybel.Molecule]
            A list of OEChem molecule instances.

        Returns
        -------
        dict
            A dictionary containing the requested properties for
            `mols`.
        """
        xtb_props = collections.defaultdict(list)

        if self.verbose:
            LOGGER.info("Now running xTB...")

        if self.progress:
            mol_progress = tqdm(mols)
        else:
            mol_progress = mols
        for mol in mol_progress:
            xtb_out = run_xtb_calc(mol, opt=self.xtbopt)
            for prop, val in xtb_out.items():
                xtb_props[prop].append(val)
        return xtb_props

    def _inv_scale(self, preds, norm_dict):
        """Inverse min-max scaling transformation

        Parameters
        ----------
        preds : np.ndarray
            Normalized predictions
        norm_dict : dict
            A dictionary containing scale and location values for
            inverse normalization.

        Returns
        -------
        numpy.ndarray
            Unnormalized predictions in their original scale
            and location.
        """
        return preds * norm_dict["scale"] + norm_dict["location"]

    def _predict_batch(self, generator, batch_size):
        """Utility method for prediction using OEChem generators
        (e.g. those used for reading sdf or xyz files)

        Parameters
        ----------
        generator : pybel.filereader
            A pybel.filereader instance
        batch_size : int
            Batch size used for prediction. Defaults to the same one
            used under `self.predict`.

        Returns
        -------
        dict
            Requested DFT-predicted properties.
        """
        preds_batch = []
        done_flag = False
        done_so_far = 0

        while not done_flag:
            mols = []
            for _ in range(batch_size):
                try:
                    mol = next(generator)
                    mols.append(mol)
                    done_so_far += 1
                except StopIteration:
                    done_flag = True
                    break

            if self.progress:
                print(f"Done computing for {done_so_far} molecules...")

            preds_batch.append(self.predict(mols, batch_size))

        pred_keys = preds_batch[0].keys()
        preds = collections.defaultdict(list)
        for pred_k in pred_keys:
            for batch in preds_batch:
                if pred_k == "charges":
                    preds[pred_k].extend(batch[pred_k])
                else:
                    preds[pred_k].extend(batch[pred_k].tolist())
            if pred_k != "charges":
                preds[pred_k] = np.array(preds[pred_k], dtype=np.float32)

        return dict(preds)

    def predict(self, input_, batch_size=32):
        """Main prediction method for DFT observables.

        Parameters
        ----------
        input_ : None
            Either a list of OEChem Molecule instances or a pybel filereader generator instance.
        batch_size : int, optional
            Batch size used for prediction, by default 32

        Returns
        -------
        dict
            Requested DFT-predicted properties.
        """
        if isinstance(input_, openbabel.pybel.Molecule):
            return self.predict([input_])

        elif isinstance(input_, list):
            mols, fatal = self._preprocess(input_)

        elif isinstance(input_, types.GeneratorType):
            return self._predict_batch(input_, batch_size)

        else:
            raise ValueError(
                f"Invalid input. Expected OEChem molecule, list or generator, but got {type(input_)}."
            )

        if self.delta:
            xtb_props = self._get_xtb_props(mols)  # TODO --> add error propagation

        data = DeltaDataset(mols)
        loader = DataLoader(data, batch_size=batch_size, shuffle=False)

        preds = {}

        for _, model_name in enumerate(self.models):
            if self.verbose:
                LOGGER.info(f"Now running network for model {model_name}...")
            model_param = MODEL_HPARAMS[model_name]
            model = EGNN(
                n_outputs=model_param.n_outputs, global_prop=model_param.global_prop
            ).eval()
            weights = get_model_weights(model_name)
            model.load_state_dict(weights)
            y_hat, g_ptr = self._get_preds(loader, model)

            if "charges" in model_name:
                atom_y_hats = []
                for batch_idx, batch_ptr in enumerate(g_ptr):
                    atom_y_hats.extend(
                        [
                            y_hat[batch_idx][batch_ptr[idx] : batch_ptr[idx + 1]]
                            for idx in range(len(batch_ptr) - 1)
                        ]
                    )
                preds[model_name] = atom_y_hats
            else:
                y_hat = np.vstack(y_hat)

                if "multitask" in model_name:
                    if "direct" in model_name:
                        y_hat = self._inv_scale(y_hat, self.norm["direct"])
                    else:
                        y_hat = self._inv_scale(y_hat, self.norm["delta"])

                preds[model_name] = y_hat

        preds_filtered = {}

        for model_name in preds.keys():
            mname = model_name.rsplit("_", maxsplit=1)[0]
            if mname == "single_energy":
                preds_filtered["E_form"] = preds[model_name].squeeze()
            elif mname == "multitask":
                for task in self.multitasks:
                    preds_filtered[task] = preds[model_name][
                        :, MULTITASK_ENDPOINTS[task]
                    ]

            elif mname == "charges":
                preds_filtered["charges"] = preds[model_name]

        if self.delta:
            for prop, delta_arr in preds_filtered.items():
                if prop == "charges":
                    preds_filtered[prop] = [
                        d_arr + np.array(xtb_arr)
                        for d_arr, xtb_arr in zip(delta_arr, xtb_props[prop])
                    ]
                else:
                    preds_filtered[prop] = delta_arr + np.array(
                        xtb_props[prop], dtype=np.float32
                    )

        # insert placeholder values where errors occurred
        preds_final = {}
        for key, val in preds_filtered.items():
            val = val.tolist() if key != "charges" else val
            temp = []
            for idx in range(len(input_)):
                if idx in fatal:
                    if key == "charges":
                        temp.append(np.array(PLACEHOLDER))
                    else:
                        temp.append(PLACEHOLDER)
                else:
                    temp.append(val.pop())
            preds_final[key] = temp
        return preds_final


if __name__ == "__main__":
    import argparse
    import pandas as pd
    import json
    from openbabel.pybel import readfile
    from delfta.utils import preds_to_lists, column_order

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mol_file", type=str, help="Path to molecule file, readable with Openbabel."
    )
    parser.add_argument(
        "--tasks",
        nargs="+",
        default=["all"],
        help="A list of tasks to predict. Available tasks include `[E_form, E_homo, E_lumo, E_gap, dipole, charges]`, by default 'all'.",
    )
    parser.add_argument(
        "--outfile",
        type=str,
        default="_default_",
        help="Path to output filename. The file extension will be generated according to --json/--csv option choice. Defaults to `delfta_predictions.json`/`delfta_predictions.csv` in the directory of `mol_file`.",
    )
    parser.add_argument(
        "--csv",
        type=bool,
        default=False,
        help="Whether to write the results as csv file instead of the default json, by default False",
    )
    parser.add_argument(
        "--delta",
        type=bool,
        default=True,
        help="Whether to use delta-learning models, by default `True`",
    )
    parser.add_argument(
        "--batch_size",
        type=int,
        default=32,
        help="Batch size used for prediction of large files",
    )
    parser.add_argument(
        "--force3d",
        type=bool,
        default=True,
        help="Whether to assign 3D coordinates to molecules lacking them, by default True",
    )
    parser.add_argument(
        "--addh",
        type=bool,
        default=True,
        help="Whether to add hydrogens to molecules lacking them, by default True",
    )
    parser.add_argument(
        "--xtbopt",
        type=bool,
        default=False,
        help="Whether to perform GFN2-xTB geometry optimization, by default False",
    )
    parser.add_argument(
        "--verbose",
        type=bool,
        default=True,
        help="Enables/disables stdout logger, by default True",
    )
    parser.add_argument(
        "--progress",
        type=bool,
        default=True,
        help="Enables/disables progress bars in prediction, by default True",
    )
    args = parser.parse_args()

    outfile = (
        os.path.join(os.path.dirname(args.mol_file), "delfta_predictions")
        if args.outfile == "_default_"
        else args.outfile
    )

    ext = os.path.splitext(args.mol_file)[1].lstrip(".")
    reader = readfile(ext, args.mol_file)

    calc = DelftaCalculator(
        tasks=args.tasks,
        delta=args.delta,
        force3d=args.force3d,
        addh=args.addh,
        xtbopt=args.xtbopt,
        verbose=args.verbose,
        progress=args.progress,
    )

    preds = calc.predict(reader, batch_size=args.batch_size)
    preds_list = preds_to_lists(preds)

    if args.csv:
        df = pd.DataFrame(preds)
        df = df[sorted(df.columns.tolist(), key=lambda x: column_order[x])]
        df.to_csv(outfile)
    else:
        with open(outfile, "w", encoding="utf-8") as f:
            json.dump(preds_list, f, ensure_ascii=False, indent=4)
