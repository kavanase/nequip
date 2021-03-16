import logging
import numpy as np
import pytest

import ase
import ase.build
import tempfile
import torch

from os.path import isfile
from ase.calculators.singlepoint import SinglePointCalculator
from torch_geometric.data import Batch

from e3nn import o3
from e3nn.util.jit import script

from nequip.data import AtomicDataDict, AtomicData
from nequip.models import EnergyModel, ForceModel
from nequip.nn import GraphModuleMixin, AtomwiseLinear

# from nequip.utils.test import assert_AtomicData_equivariant

logging.basicConfig(level=logging.DEBUG)

ALLOWED_SPECIES = [1, 8]
r_max = 3
minimal_config1 = dict(
    allowed_species=ALLOWED_SPECIES,
    irreps_edge_sh="0e + 1o",
    r_max=4,
    feature_irreps_hidden="4x0e + 4x1o",
    resnet=True,
    num_layers=2,
    num_basis=8,
    PolynomialCutoff_p=6,
    nonlinearity_type="norm",
)
minimal_config2 = dict(
    allowed_species=ALLOWED_SPECIES,
    irreps_edge_sh="0e + 1o",
    r_max=4,
    feature_embedding_irreps_out="8x0e + 8x0o + 8x1e + 8x1o",
    irreps_mid_output_block="2x0e",
    feature_irreps_hidden="4x0e + 4x1o",
)


def force_model(**kwargs):
    energy_model = EnergyModel(**kwargs)
    return ForceModel(energy_model)


devices = (
    [torch.device("cuda"), torch.device("cpu")]
    if torch.cuda.is_available()
    else [torch.device("cpu")]
)


@pytest.fixture(scope="module", params=[minimal_config1, minimal_config2])
def config(request):
    return request.param


@pytest.fixture(
    params=[
        (force_model, AtomicDataDict.FORCE_KEY),
        (EnergyModel, AtomicDataDict.TOTAL_ENERGY_KEY),
    ]
)
def model(request, config):
    torch.manual_seed(0)
    np.random.seed(0)
    builder, out_field = request.param
    return builder(**config), out_field


@pytest.fixture(
    scope="module",
    params=(
        [torch.device("cuda"), torch.device("cpu")]
        if torch.cuda.is_available()
        else [torch.device("cpu")]
    ),
)
def device(request):
    return request.param


@pytest.fixture(scope="module")
def data(float_tolerance):
    torch.manual_seed(0)
    np.random.seed(0)
    data = AtomicData.from_ase(get_atoms(), r_max=3)
    return data


@pytest.fixture(scope="module")
def batch(data):
    torch.manual_seed(0)
    np.random.seed(0)
    data1 = AtomicData.from_ase(get_atoms(), r_max=3)
    data2 = AtomicData.from_ase(get_atoms(), r_max=3)
    batch = Batch.from_data_list([data1, data2])
    return batch


def get_atoms():
    atoms = []
    atoms = ase.build.molecule("H2O")
    atoms.rattle(stdev=1)
    atoms.calc = SinglePointCalculator(
        atoms,
        **{
            "forces": np.random.random((len(atoms), 3)),
            "free_energy": np.random.random(1),
        },
    )
    return atoms


class TestWorkflow:
    """
    test class methods
    """

    def test_init(self, model):
        instance, _ = model
        assert isinstance(instance, GraphModuleMixin)

    def test_jit(self, model, data, device):
        instance, out_field = model
        instance = instance.to(device=device)
        data = data.to(device)
        model_script = script(instance)
        assert torch.allclose(
            instance(AtomicData.to_AtomicDataDict(data))[out_field],
            model_script(AtomicData.to_AtomicDataDict(data))[out_field],
        )

    def test_submods(self):
        model = EnergyModel(**minimal_config2)
        assert isinstance(model.feature_embedding, AtomwiseLinear)
        true_irreps = o3.Irreps(minimal_config2["feature_embedding_irreps_out"])
        assert (
            model.feature_embedding.irreps_out[model.feature_embedding.out_field]
            == true_irreps
        )
        # Make sure it propagates
        assert model.convnet.irreps_in[model.feature_embedding.out_field] == true_irreps

    def test_forward(self, model, data, device):
        instance, out_field = model
        instance.to(device)
        data = data.to(device)
        output = instance(AtomicData.to_AtomicDataDict(data))
        assert out_field in output

    def test_saveload(self, model):
        with tempfile.NamedTemporaryFile(suffix=".pth") as tmp:
            instance, _ = model
            torch.save(instance, tmp.name)
            assert isfile(tmp.name)

            new_model = torch.load(tmp.name)
            assert isinstance(new_model, type(instance))


class TestGradient:
    def test_numeric_gradient(self, config, data, device, float_tolerance):

        model = force_model(**config)
        model.to(device)
        output = model(AtomicData.to_AtomicDataDict(data.to(device)))

        forces = output[AtomicDataDict.FORCE_KEY]

        epsilon = torch.as_tensor(1e-3)
        epsilon2 = torch.as_tensor(2e-3)
        iatom = 1
        for idir in range(3):
            data[AtomicDataDict.POSITIONS_KEY][iatom, idir] += epsilon
            output = model(AtomicData.to_AtomicDataDict(data.to(device)))
            e_plus = output[AtomicDataDict.TOTAL_ENERGY_KEY]

            data[AtomicDataDict.POSITIONS_KEY][iatom, idir] -= epsilon2
            output = model(AtomicData.to_AtomicDataDict(data.to(device)))
            e_minus = output[AtomicDataDict.TOTAL_ENERGY_KEY]

            numeric = -(e_plus - e_minus) / epsilon2
            analytical = forces[iatom, idir]
            print(numeric.item(), analytical.item())
            assert torch.isclose(numeric, analytical, atol=2e-2) or torch.isclose(
                numeric, analytical, rtol=5e-2
            )


class TestAutoGradient:
    def test_cross_frame_grad(self, config, batch):
        device = "cpu"
        energy_model = EnergyModel(**config)
        energy_model.to(device)
        data = AtomicData.to_AtomicDataDict(batch.to(device))
        data[AtomicDataDict.POSITIONS_KEY].requires_grad = True

        output = energy_model(data)
        grads = torch.autograd.grad(
            outputs=output[AtomicDataDict.TOTAL_ENERGY_KEY][-1],
            inputs=data[AtomicDataDict.POSITIONS_KEY],
            allow_unused=True,
        )[0]

        last_frame_n_atom = batch.ptr[-1] - batch.ptr[-2]

        in_frame_grad = grads[-last_frame_n_atom:]
        cross_frame_grad = grads[:-last_frame_n_atom]

        assert cross_frame_grad.abs().max().item() == 0
        assert in_frame_grad.abs().max().item() > 0


# class TestEquivariance:
# @loop_energy1
# @loop_cpu
# def test_energy(self, energy_model, device):

#     energy_model.to(device)

#     for i in range(2):
#         data = AtomicData.from_ase(get_atoms(), r_max=3)
#         data = AtomicData.to_AtomicDataDict(data.to(device))
#         assert_AtomicData_equivariant(
#             func=energy_model,
#             data_in=data,
#             func_irreps_out="1x0e",
#             out_field=AtomicDataDict.TOTAL_ENERGY_KEY,
#             randomize_features=True,
#         )

# @loop_force1
# @loop_cpu
# def test_force(self, force_model, device):

#     force_model.to(device)
#     for i in range(2):
#         data = AtomicData.from_ase(get_atoms(), r_max=3)
#         data = AtomicData.to_AtomicDataDict(data.to(device))
#         assert_AtomicData_equivariant(
#             func=force_model,
#             data_in=data,
#             func_irreps_out="1x1o",
#             out_field=AtomicDataDict.FORCE_KEY,
#             randomize_features=True,
#         )
