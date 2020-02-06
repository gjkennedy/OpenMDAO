import os

import unittest
import numpy as np
from six.moves import cStringIO

import openmdao.api as om

from openmdao.utils.mpi import MPI
from openmdao.utils.array_utils import evenly_distrib_idxs
from openmdao.utils.assert_utils import assert_rel_error
from openmdao.utils.general_utils import remove_whitespace

from openmdao.test_suite.groups.parallel_groups import FanOutGrouped

try:
    from openmdao.vectors.petsc_vector import PETScVector
except ImportError:
    PETScVector = None


class DistributedAdder(om.ExplicitComponent):
    """
    Distributes the work of adding 10 to every item in the param vector
    """

    def initialize(self):
        self.options['distributed'] = True

        self.options.declare('size', types=int, default=1,
                             desc="Size of input and output vectors.")

    def setup(self):
        """
        specify the local sizes of the variables and which specific indices this specific
        distributed component will handle. Indices do NOT need to be sequential or
        contiguous!
        """
        comm = self.comm
        rank = comm.rank

        # NOTE: evenly_distrib_idxs is a helper function to split the array
        #       up as evenly as possible
        sizes, offsets = evenly_distrib_idxs(comm.size,self.options['size'])
        local_size, local_offset = sizes[rank], offsets[rank]

        start = local_offset
        end = local_offset + local_size

        self.add_input('x', val=np.zeros(local_size, float),
                       src_indices=np.arange(start, end, dtype=int))
        self.add_output('y', val=np.zeros(local_size, float))

    def compute(self, inputs, outputs):

        # NOTE: Each process will get just its local part of the vector
        # print('process {0:d}: {1}'.format(self.comm.rank, params['x'].shape))

        outputs['y'] = inputs['x'] + 10.


class Summer(om.ExplicitComponent):
    """
    Aggregation component that collects all the values from the distributed
    vector addition and computes a total
    """

    def initialize(self):
        self.options.declare('size', types=int, default=1,
                             desc="Size of input and output vectors.")

    def setup(self):
        # NOTE: this component depends on the full y array, so OpenMDAO
        #       will automatically gather all the values for it
        self.add_input('y', val=np.zeros(self.options['size']))
        self.add_output('sum', 0.0, shape=1)

    def compute(self, inputs, outputs):
        outputs['sum'] = np.sum(inputs['y'])


@unittest.skipUnless(MPI, "MPI is required.")
@unittest.skipIf(PETScVector is None, "PETSc is required.")
@unittest.skipIf(os.environ.get("TRAVIS"), "Unreliable on Travis CI.")
class DistributedListVarsTest(unittest.TestCase):

    N_PROCS = 2

    def test_distributed_array_list_vars(self):

        size = 100  # how many items in the array

        prob = om.Problem()

        prob.model.add_subsystem('des_vars', om.IndepVarComp('x', np.ones(size)), promotes=['x'])
        prob.model.add_subsystem('plus', DistributedAdder(size=size), promotes=['x', 'y'])
        prob.model.add_subsystem('summer', Summer(size=size), promotes=['y', 'sum'])

        prob.setup()

        prob['x'] = np.arange(size)

        prob.run_driver()

        stream = cStringIO()
        inputs = sorted(prob.model.list_inputs(values=True, print_arrays=True, out_stream=stream))
        self.assertEqual(inputs[0][0], 'plus.x')
        self.assertEqual(inputs[1][0], 'summer.y')
        self.assertEqual(inputs[0][1]['value'].size, 50)  # should only return half that is local
        self.assertEqual(inputs[1][1]['value'].size, 100)

        text = stream.getvalue()
        if prob.comm.rank:  # Only rank 0 prints
            self.assertEqual(len(text), 0)
        else:
            self.assertEqual(text.count('value'), 3)
            self.assertEqual(text.count('\nmodel'), 1)
            self.assertEqual(text.count('\n  plus'), 1)
            self.assertEqual(text.count('\n    x'), 1)
            self.assertEqual(text.count('\n  summer'), 1)
            self.assertEqual(text.count('\n    y'), 1)
            # make sure all the arrays written have 100 elements in them
            self.assertEqual(len(text.split('[')[1].split(']')[0].split()), 100)
            self.assertEqual(len(text.split('[')[2].split(']')[0].split()), 100)

        stream = cStringIO()
        outputs = sorted(prob.model.list_outputs(values=True,
                                                 units=True,
                                                 shape=True,
                                                 bounds=True,
                                                 residuals=True,
                                                 scaling=True,
                                                 hierarchical=True,
                                                 print_arrays=True,
                                                 out_stream=stream))
        self.assertEqual(outputs[0][0], 'des_vars.x')
        self.assertEqual(outputs[1][0], 'plus.y')
        self.assertEqual(outputs[2][0], 'summer.sum')
        self.assertEqual(outputs[0][1]['value'].size, 100)
        self.assertEqual(outputs[1][1]['value'].size, 50)
        self.assertEqual(outputs[2][1]['value'].size, 1)

        text = stream.getvalue()
        if prob.comm.rank:  # Only rank 0 prints
            self.assertEqual(len(text), 0)
        else:
            self.assertEqual(text.count('value'), 3)
            self.assertEqual(text.count('\n  des_vars'), 1)
            self.assertEqual(text.count('\n    x'), 1)
            self.assertEqual(text.count('\n  plus'), 1)
            self.assertEqual(text.count('\n    y'), 1)
            self.assertEqual(text.count('\n  summer'), 1)
            self.assertEqual(text.count('\n    sum'), 1)
            # make sure all the arrays written have 100 elements in them
            self.assertEqual(len(text.split('[')[1].split(']')[0].split()), 100)
            self.assertEqual(len(text.split('[')[2].split(']')[0].split()), 100)
            self.assertEqual(len(text.split('[')[3].split(']')[0].split()), 100)
            self.assertEqual(len(text.split('[')[4].split(']')[0].split()), 100)

    def test_distributed_list_vars(self):

        from openmdao.utils.general_utils import set_pyoptsparse_opt

        # check that pyoptsparse is installed. if it is, try to use SLSQP.
        OPT, OPTIMIZER = set_pyoptsparse_opt('SLSQP')

        if OPTIMIZER:
            from openmdao.drivers.pyoptsparse_driver import pyOptSparseDriver
        else:
            raise unittest.SkipTest("pyOptSparseDriver is required.")

        class Mygroup(om.Group):

            def setup(self):
                self.add_subsystem('indep_var_comp', om.IndepVarComp('x'), promotes=['*'])
                self.add_subsystem('Cy', om.ExecComp('y=2*x'), promotes=['*'])
                self.add_subsystem('Cc', om.ExecComp('c=x+2'), promotes=['*'])

                self.add_design_var('x')
                self.add_constraint('c', lower=-3.)

        prob = om.Problem()

        prob.model.add_subsystem('par', om.ParallelGroup())

        prob.model.par.add_subsystem('G1', Mygroup())
        prob.model.par.add_subsystem('G2', Mygroup())

        prob.model.add_subsystem('Obj', om.ExecComp('obj=y1+y2'))

        prob.model.connect('par.G1.y', 'Obj.y1')
        prob.model.connect('par.G2.y', 'Obj.y2')

        prob.model.add_objective('Obj.obj')

        prob.driver = pyOptSparseDriver()
        prob.driver.options['optimizer'] = 'SLSQP'

        prob.setup()
        prob.run_driver()
        prob.cleanup()

        stream = cStringIO()
        inputs = sorted(prob.model.list_inputs(values=True, print_arrays=True, out_stream=stream))
        self.assertEqual(inputs[0][0], 'Obj.y1')
        self.assertEqual(inputs[1][0], 'Obj.y2')
        if prob.comm.rank:  # Only rank 0 prints
            self.assertEqual(inputs[2][0], 'par.G2.Cc.x')
            self.assertEqual(inputs[3][0], 'par.G2.Cy.x')
        else:
            self.assertEqual(inputs[2][0], 'par.G1.Cc.x')
            self.assertEqual(inputs[3][0], 'par.G1.Cy.x')
        self.assertTrue('value' in inputs[0][1])
        self.assertEqual(4, len(inputs))

        text = stream.getvalue()
        if prob.comm.rank:  # Only rank 0 prints
            self.assertEqual(len(text), 0)
        else:
            self.assertEqual(1, text.count("6 Input(s) in 'model'"), 1)
            self.assertEqual(1, text.count('value'))
            self.assertEqual(1, text.count('  par'))
            self.assertEqual(1, text.count('    G1'))
            self.assertEqual(1, text.count('    G2'))
            self.assertEqual(2, text.count('      Cy'))
            self.assertEqual(2, text.count('      Cc'))
            self.assertEqual(4, text.count('        x'))
            self.assertEqual(1, text.count('  Obj'))
            self.assertEqual(1, text.count('    y1'))
            self.assertEqual(1, text.count('    y2'))

        stream = cStringIO()
        outputs = sorted(prob.model.list_outputs(values=True,
                                                 units=True,
                                                 shape=True,
                                                 bounds=True,
                                                 residuals=True,
                                                 scaling=True,
                                                 hierarchical=True,
                                                 print_arrays=True,
                                                 out_stream=stream))
        self.assertEqual(outputs[0][0], 'Obj.obj')
        if prob.comm.rank:  # outputs only return what is on their proc
            self.assertEqual(outputs[1][0], 'par.G2.Cc.c')
            self.assertEqual(outputs[2][0], 'par.G2.Cy.y')
            self.assertEqual(outputs[3][0], 'par.G2.indep_var_comp.x')
        else:
            self.assertEqual(outputs[1][0], 'par.G1.Cc.c')
            self.assertEqual(outputs[2][0], 'par.G1.Cy.y')
            self.assertEqual(outputs[3][0], 'par.G1.indep_var_comp.x')
        self.assertEqual(4, len(outputs))
        self.assertTrue('value' in outputs[0][1])
        self.assertTrue('units' in outputs[0][1])

        text = stream.getvalue()
        if prob.comm.rank:  # Only rank 0 prints
            self.assertEqual(len(text), 0)
        else:
            self.assertEqual(1, text.count("7 Explicit Output(s) in 'model'"))
            self.assertEqual(1, text.count('value'))
            self.assertEqual(1, text.count('units'))
            self.assertEqual(1, text.count('  par'))
            self.assertEqual(1, text.count('    G1'))
            self.assertEqual(1, text.count('    G2'))
            self.assertEqual(2, text.count('      Cy'))
            self.assertEqual(2, text.count('      Cc'))
            self.assertEqual(2, text.count('      indep_var_comp'))
            self.assertEqual(2, text.count('        x'))
            self.assertEqual(2, text.count('        y'))
            self.assertEqual(2, text.count('        c'))
            self.assertEqual(1, text.count('  Obj'))
            self.assertEqual(1, text.count('    obj'))

    def test_parallel_list_vars(self):
        np.set_printoptions(linewidth=1024)

        prob = om.Problem(FanOutGrouped())

        # add another subsystem with similar prefix
        prob.model.add_subsystem('sub2', om.ExecComp(['y=x']))
        prob.model.connect('iv.x', 'sub2.x')

        prob.setup()
        prob.run_model()

        #
        # list inputs, not hierarchical
        #
        stream = cStringIO()
        prob.model.list_inputs(values=True, hierarchical=False, out_stream=stream)

        if prob.comm.rank == 0:  # Only rank 0 prints
            text = stream.getvalue().split('\n')

            expected = [
                "6 Input(s) in 'model'",
                '---------------------',
                '',
                'varname   value',
                '--------  -----',
                'c1.x',
                'sub.c2.x',
                'sub.c3.x',
                'c2.x',
                'c3.x',
                'sub2.x'
            ]

            for i, line in enumerate(expected):
                if line and not line.startswith('-'):
                    self.assertTrue(text[i].startswith(line),
                                    '\nExpected: %s\nReceived: %s\n' % (line, text[i]))

        #
        # list inputs, hierarchical
        #
        stream = cStringIO()
        prob.model.list_inputs(values=True, hierarchical=True, out_stream=stream)

        if prob.comm.rank == 0:
            text = stream.getvalue().split('\n')

            expected = [
                "6 Input(s) in 'model'",
                '---------------------',
                '',
                'varname  value',
                '-------  -----',
                'model',
                '  c1',
                '    x',
                '  sub',
                '    c2',
                '      x',
                '    c3',
                '      x',
                '  c2',
                '    x',
                '  c3',
                '    x',
                '  sub2',
                '    x'
            ]

            for i, line in enumerate(expected):
                if line and not line.startswith('-'):
                    self.assertTrue(text[i].startswith(line),
                                    '\nExpected: %s\nReceived: %s\n' % (line, text[i]))

        #
        # list outputs, not hierarchical
        #
        stream = cStringIO()
        prob.model.list_outputs(values=True, residuals=True, hierarchical=False, out_stream=stream)

        if prob.comm.rank == 0:
            text = stream.getvalue().split('\n')

            expected = [
                "7 Explicit Output(s) in 'model'",
                '-------------------------------',
                '',
                'varname   value  resids',
                '--------  -----  ------',
                'iv.x',
                'c1.y',
                'sub.c2.y',
                'sub.c3.y',
                'c2.y',
                'c3.y',
                'sub2.y',
                '',
                '',
                "0 Implicit Output(s) in 'model'",
                '-------------------------------',
            ]

            for i, line in enumerate(expected):
                if line and not line.startswith('-'):
                    self.assertTrue(text[i].startswith(line),
                                    '\nExpected: %s\nReceived: %s\n' % (line, text[i]))

        #
        # list outputs, hierarchical
        #
        stream = cStringIO()
        prob.model.list_outputs(values=True, residuals=True, hierarchical=True, out_stream=stream)

        if prob.comm.rank == 0:
            text = stream.getvalue().split('\n')

            expected = [
                "7 Explicit Output(s) in 'model'",
                '-------------------------------',
                '',
                'varname  value  resids',
                '-------  -----  ------',
                'model',
                '  iv',
                '    x',
                '  c1',
                '    y',
                '  sub',
                '    c2',
                '      y',
                '    c3',
                '      y',
                '  c2',
                '    y',
                '  c3',
                '    y',
                '  sub2',
                '    y',
                '',
                '',
                "0 Implicit Output(s) in 'model'",
                '-------------------------------',
            ]

            for i, line in enumerate(expected):
                if line and not line.startswith('-'):
                    self.assertTrue(text[i].startswith(line),
                                    '\nExpected: %s\nReceived: %s\n' % (line, text[i]))

    def test_distribcomp_list_vars(self):
        from openmdao.test_suite.components.distributed_components import DistribComp, Summer

        np.set_printoptions(linewidth=1024)

        size = 15

        model = om.Group()
        model.add_subsystem("indep", om.IndepVarComp('x', np.zeros(size)))
        model.add_subsystem("C2", DistribComp(size=size))
        model.add_subsystem("C3", Summer(size=size))

        model.connect('indep.x', 'C2.invec')
        model.connect('C2.outvec', 'C3.invec')

        prob = om.Problem(model)
        prob.setup()

        # prior to model execution, the global shape of a distributed variable is not available
        # and only the local portion of the value is available
        stream = cStringIO()
        model.C2.list_inputs(hierarchical=False, shape=True, global_shape=True,
                             print_arrays=True, out_stream=stream)

        if prob.comm.rank == 0:
            text = stream.getvalue().split('\n')

            expected = [
                "1 Input(s) in 'C2'",
                '------------------',
                '',
                'varname  value                 shape  global_shape',
                '-------  --------------------  -----  ------------',
                'invec    |2.8284271247461903|  (8,)   Unavailable ',
                '         value:',
                '         array([1., 1., 1., 1., 1., 1., 1., 1.])'
            ]

            for i, line in enumerate(expected):
                if line and not line.startswith('-'):
                    self.assertEqual(remove_whitespace(text[i]), remove_whitespace(line),
                                     '\nExpected: %s\nReceived: %s\n' % (line, text[i]))

        stream = cStringIO()
        model.C2.list_outputs(hierarchical=False, shape=True, global_shape=True,
                              print_arrays=True, out_stream=stream)

        if prob.comm.rank == 0:
            text = stream.getvalue().split('\n')

            expected = [
                "1 Explicit Output(s) in 'C2'",
                '----------------------------',
                '',
                'varname  value                 shape  global_shape',
                '-------  --------------------  -----  ------------',
                'outvec   |2.8284271247461903|  (8,)   Unavailable ',
                '         value:',
                '         array([1., 1., 1., 1., 1., 1., 1., 1.])'
            ]

            for i, line in enumerate(expected):
                if line and not line.startswith('-'):
                    self.assertEqual(remove_whitespace(text[i]), remove_whitespace(line),
                                     '\nExpected: %s\nReceived: %s\n' % (line, text[i]))

        # run the model
        prob['indep.x'] = np.ones(size)
        prob.run_model()

        # after model execution, the global shape of a distributed variable is available
        # and the complete global value is available
        stream = cStringIO()
        model.C2.list_inputs(hierarchical=False, shape=True, global_shape=True,
                             print_arrays=True, out_stream=stream)

        if prob.comm.rank == 0:
            text = stream.getvalue().split('\n')

            expected = [
                "1 Input(s) in 'C2'",
                '------------------',
                '',
                'varname   value                shape  global_shape',
                '--------  -------------------  -----  ------------',
                'C2.invec  |3.872983346207417|  (8,)   (15,)       ',
                '          value:',
                '          array([1., 1., 1., 1., 1., 1., 1., 1., 1., 1., 1., 1., 1., 1., 1.])'
            ]
            for i, line in enumerate(expected):
                if line and not line.startswith('-'):
                    self.assertEqual(remove_whitespace(text[i]), remove_whitespace(line),
                                     '\nExpected: %s\nReceived: %s\n' % (line, text[i]))

        stream = cStringIO()
        model.C2.list_outputs(hierarchical=False, shape=True, global_shape=True,
                              print_arrays=True, out_stream=stream)

        if prob.comm.rank == 0:
            text = stream.getvalue().split('\n')

            expected = [
                "1 Explicit Output(s) in 'C2'",
                '----------------------------',
                '',
                'varname    value                shape  global_shape',
                '---------  -------------------  -----  ------------',
                'C2.outvec  |9.746794344808963|  (8,)   (15,)       ',
                '           value:',
                '           array([ 2.,  2.,  2.,  2.,  2.,  2.,  2.,  2., -3., -3., -3., -3., -3., -3., -3.])'
            ]
            for i, line in enumerate(expected):
                if line and not line.startswith('-'):
                    self.assertEqual(remove_whitespace(text[i]), remove_whitespace(line),
                                     '\nExpected: %s\nReceived: %s\n' % (line, text[i]))

        # note that the shape of the input variable for the non-distributed Summer component
        # is different on each processor, use the all_procs argument to display on all processors
        stream = cStringIO()
        model.C3.list_inputs(hierarchical=False, shape=True, global_shape=True, all_procs=True,
                             print_arrays=True, out_stream=stream)

        text = stream.getvalue().split('\n')

        if prob.comm.rank == 0:
            norm = '|5.656854249492381|'
            shape = (8,)
            value = '[2., 2., 2., 2., 2., 2., 2., 2.]'
        else:
            norm = '|7.937253933193772|'
            shape = (7,)
            value = '[-3., -3., -3., -3., -3., -3., -3.]'

        expected = [
            "1 Input(s) in 'C3'",
            '------------------',
            '',
            'varname   value                shape  global_shape',
            '--------  -------------------  -----  ------------',
            'C3.invec  {}  {}   {}        '.format(norm, shape, shape),
            '          value:',
            '          array({})'.format(value),
        ]

        for i, line in enumerate(expected):
            if line and not line.startswith('-'):
                self.assertEqual(remove_whitespace(text[i]), remove_whitespace(line),
                                 '\nExpected: %s\nReceived: %s\n' % (line, text[i]))

        assert_rel_error(self, prob['C3.out'], -5.)


@unittest.skipUnless(PETScVector, "PETSc is required.")
@unittest.skipUnless(MPI, "MPI is required.")
class MPIFeatureTests(unittest.TestCase):

    N_PROCS = 2

    def test_distribcomp_list_feature(self):
        import numpy as np
        import openmdao.api as om
        from openmdao.test_suite.components.distributed_components import DistribComp, Summer

        size = 15

        model = om.Group()
        model.add_subsystem("indep", om.IndepVarComp('x', np.zeros(size)))
        model.add_subsystem("C2", DistribComp(size=size))
        model.add_subsystem("C3", Summer(size=size))

        model.connect('indep.x', 'C2.invec')
        model.connect('C2.outvec', 'C3.invec')

        prob = om.Problem(model)
        prob.setup()

        # prior to model execution, the global shape of a distributed variable is not available
        # and only the local portion of the value is available
        model.C2.list_inputs(hierarchical=False, shape=True, global_shape=True, print_arrays=True)
        model.C2.list_outputs(hierarchical=False, shape=True, global_shape=True, print_arrays=True)

        prob['indep.x'] = np.ones(size)
        prob.run_model()

        # after model execution, the global shape of a distributed variable is available
        # and the complete global value is available
        model.C2.list_inputs(hierarchical=False, shape=True, global_shape=True, print_arrays=True)
        model.C2.list_outputs(hierarchical=False, shape=True, global_shape=True, print_arrays=True)

        # note that the shape of the input variable for the non-distributed Summer component
        # is different on each processor, use the all_procs argument to display on all processors
        model.C3.list_inputs(hierarchical=False, shape=True, global_shape=True, print_arrays=True, all_procs=True)

        assert_rel_error(self, prob['C3.out'], -5.)


if __name__ == "__main__":
    from openmdao.utils.mpi import mpirun_tests
    mpirun_tests()
