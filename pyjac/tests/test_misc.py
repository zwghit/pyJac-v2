# test various functions in the utils function, or elsewhere

from parameterized import parameterized
import numpy as np

try:
    from scipy.sparse import csr_matrix, csc_matrix
except:
    csr_matrix = None
    csc_matrix = None

from pyjac.utils import enum_to_string, listify
from pyjac.loopy_utils.loopy_utils import JacobianType, JacobianFormat
from pyjac.libgen import build_type
from pyjac.tests.test_utils import skipif, dense_to_sparse_indicies


@parameterized([(JacobianType.exact, 'exact'),
                (JacobianType.approximate, 'approximate'),
                (JacobianType.finite_difference, 'finite_difference'),
                (JacobianFormat.sparse, 'sparse'),
                (JacobianFormat.full, 'full'),
                (build_type.chem_utils, 'chem_utils'),
                (build_type.species_rates, 'species_rates'),
                (build_type.jacobian, 'jacobian')])
def test_enum_to_string(enum, string):
    assert enum_to_string(enum) == string


@parameterized([('a', ['a']),
                ([1, 2, 3], [1, 2, 3]),
                ((1, 2, 'a'), [1, 2, 'a']),
                (3, [3])])
def test_listify(value, expected):
    assert listify(value) == expected


@parameterized([(
    (1024, 4, 4), lambda y, z: y + z <= 4, [np.arange(4), np.arange(4)], (1, 2)), (
    (1024, 6, 6), lambda x, y: (x + y) % 3 != 0, [np.arange(3), np.arange(6)],
        (1, 2)), (
    (1024, 10, 10), lambda x, y: x == 0, [np.array([0], np.int32), np.arange(6)],
        (1, 2)), (
    (1024, 10, 10), lambda x, y: (x & y) != 0, [
        slice(None), np.arange(4, 10), np.arange(6)], -1)
    ])
@skipif(csr_matrix is None, 'scipy missing')
def test_dense_to_sparse_indicies(shape, sparse, mask, axes):
    for order in ['C', 'F']:
        # create matrix
        arr = np.arange(1, np.prod(shape) + 1).reshape(shape, order=order)

        def __slicer(x, y):
            slicer = [slice(None)] * arr.ndim
            slicer[1:] = x, y
            return slicer

        def apply_sparse(x, y):
            arr[__slicer(*np.where(~sparse(x, y)))] = 0

        # sparsify
        np.fromfunction(apply_sparse, arr.shape[1:], dtype=np.int32)
        matrix = csr_matrix if order == 'C' else csc_matrix
        matrix = matrix(arr[0])

        # next, create a sparse copy of the matrix
        sparse_arr = np.zeros((arr.shape[0], matrix.nnz), dtype=arr.dtype)
        it = np.nditer(np.empty(shape[1:]), flags=['multi_index'], order=order)
        i = 0
        while not it.finished:
            if not sparse(*it.multi_index):
                it.iternext()
                continue

            sparse_arr[:, i] = arr[__slicer(*it.multi_index)]
            it.iternext()
            i += 1

        # get the sparse indicies
        row, col = (matrix.indices, matrix.indptr) if order == 'C' \
            else (matrix.indptr, matrix.indices)
        sparse_axes, sparse_inds = dense_to_sparse_indicies(
            mask, axes, row, col, order)
        sparse_inds = sparse_inds[-1]

        # and check
        it = np.nditer(np.empty(shape[1:]), flags=['multi_index'], order=order)
        i = 0
        while not it.finished:
            if not sparse(*it.multi_index):
                it.iternext()
                continue

            if axes == -1:
                if not (it.multi_index[0] in mask[-2] and
                        it.multi_index[1] == mask[-1][np.where(
                            it.multi_index[0] == mask[-2])]):
                    it.iternext()
                    continue

            if not (it.multi_index[0] in mask[-2] and it.multi_index[1] in mask[-1]):
                it.iternext()
                continue

            # check that the sparse indicies match what we expect
            assert np.all(sparse_arr[:, sparse_inds[i]] == arr[__slicer(
                          *it.multi_index)])
            it.iternext()
            i += 1
