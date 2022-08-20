from dolfin import UserExpression, CompiledExpression, Cell, Mesh, dot, \
                   sym, nabla_grad, inner, conditional, \
                   Vector, as_backend_type, VectorSpaceBasis, \
                   compile_cpp_code, MeshFunction
from scipy.interpolate import splev
import numpy as np
import sys, math

# Functions for boundary conditions
class Inflow(UserExpression):

    def __init__(self, param, mesh, **kwargs):
        super(Inflow, self).__init__(**kwargs)
        self.param = param
        self.mesh = mesh

    def eval_cell(self, values, x, ufc_cell):

        # Create DOLFIN Cell
        cell = Cell(self.mesh, ufc_cell.index)
        
        # Get normal for current facet
        assert(ufc_cell.local_facet >= 0)
        n = cell.normal(ufc_cell.local_facet)
        
        # Compute boundary value
        t = self.param["time"].t
        period = self.param["period"]
        nm = self.param["nm"].cycle
        Area = self.param["Area"]
        Vsc = self.param["Vsc"]
        Tsc = self.param["Tsc"]
        func = self.param["func"]

        val = (splev(t*Tsc - nm*period*Tsc, func)/Area)/Vsc
        values[0] = -n.x()*val
        values[1] = -n.y()*val
        values[2] = -n.z()*val 

    def value_shape(self):
        return (3,)


code = '''
#include <pybind11/pybind11.h>
#include <pybind11/eigen.h>
namespace py = pybind11;

#include <dolfin/function/Expression.h>
#include <dolfin/mesh/MeshFunction.h>
#include <dolfin/mesh/Cell.h>
#include <dolfin/geometry/Point.h>

class Inflow : public dolfin::Expression
{
public:
    
    double v;
    std::shared_ptr<dolfin::MeshFunction<std::size_t>> cell_data;

    Inflow(double v_, std::shared_ptr<dolfin::MeshFunction<std::size_t>> cell_data_) : Expression(3) {
        v = v_;
        cell_data = cell_data_;
    }

    void eval(Eigen::Ref<Eigen::VectorXd> values, Eigen::Ref<const Eigen::VectorXd> x, const ufc::cell& c) const override
    {
        assert(cell_data);
        const dolfin::Cell cell(*cell_data->mesh(), c.index);
        
        assert(c.local_facet >= 0);
        dolfin::Point n = cell.normal(c.local_facet);

        values[0] = -n.x()*v;  
        values[1] = -n.y()*v;
        values[2] = -n.z()*v;
    }
};

PYBIND11_MODULE(SIGNATURE, m)
{
  py::class_<Inflow, std::shared_ptr<Inflow>, dolfin::Expression>(m, "Inflow")
    .def(py::init<double, std::shared_ptr<dolfin::MeshFunction<std::size_t>>>())
    .def_readwrite("v", &Inflow::v)
    .def_readwrite("cell_data", &Inflow::cell_data);
}
'''



class Outflow(UserExpression):

    def __init__(self, param, mesh, **kwargs):
        super(Outflow, self).__init__(**kwargs)
        self.param = param
        self.mesh = mesh

    def eval_cell(self, values, x, ufc_cell):

        # Create DOLFIN Cell
        cell = Cell(self.mesh, ufc_cell.index)
        
        # Get normal for current facet
        assert(ufc_cell.local_facet >= 0)
        n = cell.normal(ufc_cell.local_facet)
        
        # Compute boundary value
        t = self.param["time"].t
        period = self.param["period"]
        nm = self.param["nm"].cycle
        Area = self.param["Area"]
        Vsc = self.param["Vsc"]
        Tsc = self.param["Tsc"] 
        func = self.param["func"]

        val = splev(t*Tsc - nm*period*Tsc, func)*133.322/(1060*Vsc*Vsc)
        values[0] = val

    def value_shape(self):
        return ()

class Temperature_balloon(UserExpression):

    def __init__(self, param, mesh, **kwargs):
        super(Temperature_balloon, self).__init__(**kwargs)
        self.param = param
        self.mesh = mesh

    def eval_cell(self, values, x,  ufc_cell):

        # Create DOLFIN Cell
        cell = Cell(self.mesh, ufc_cell.index)

        # Get normal for current facet
        assert(ufc_cell.local_facet >= 0)
        n = cell.normal(ufc_cell.local_facet)

        # Compute boundary value
        t = self.param["time"].t
        Tsc = self.param["Tsc"]
        func = self.param["func"]

        val = splev(t*Tsc, func)
        values[0] = val

    def value_shape(self):
        return ()




# Function to create nullspace
def attach_nullspace(Ap, x_, Q):

    """Create null space basis object and attach to Krylov solver."""
    null_vec = Vector(x_.vector())
    Q.dofmap().set(null_vec, 1.0)
    null_vec *= 1.0 / null_vec.norm('l2')
    Aa = as_backend_type(Ap)
    null_space = VectorSpaceBasis([null_vec])
    Aa.set_nullspace(null_space)
    Aa.null_space = null_space
    return null_space




# Function to calculate denominator for courant number
def DENO(u_, Mpi, h_f_X):

    DN_local = 0
    NM_local = 0
    mesh = u_.function_space().mesh()
    vertex_values_h_f_X = h_f_X.compute_vertex_values(mesh)
    vertex_mag_u = np.zeros(len(vertex_values_h_f_X))

    for i in range(u_.geometric_dimension()):
        vertex_values_u = u_.sub(i).compute_vertex_values(mesh)
        DN_local += np.max(np.abs(vertex_values_u / vertex_values_h_f_X))
        vertex_mag_u += np.square(vertex_values_u)

    NM_local =  np.max(np.sqrt(vertex_mag_u)*vertex_values_h_f_X)

    DN = Mpi.Max(DN_local)
    NM = Mpi.Max(NM_local)

    return DN, NM




# Degrees of freedom
def Calc_total_DOF(Mpi, **kwargs):

    DOFS = dict()
    for key, value in kwargs.items():
        
        dof = 0
        for i in value:
            dof += Mpi.Sum(i.vector().get_local().size)
        DOFS[key] = dof

    return DOFS





# Other miscellaneous functions

# symmetric gradient
def epsilon(u):
    
    return sym(nabla_grad(u))

# Viscous dissipation source term (for energy equation)
def Qf(u, Ec, Re):
    
    return inner(((2*Ec)/Re)*epsilon(u), nabla_grad(u))

# Perfusion equation (quadratic)
def PFE(Tf_n): 

    return conditional(Tf_n >= 0.725, (6.18*Tf_n*Tf_n) - (7.39*Tf_n) + 2.21, 0.1)  

# Returns a value rounded down to a specific number of decimal places
def round_decimals_down(number:float, decimals:int=8):

    if not isinstance(decimals, int):
        raise TypeError("decimal places must be an integer")
    elif decimals < 0:
        raise ValueError("decimal places has to be 0 or more")
    elif decimals == 0:
        return math.floor(number)

    factor = 10 ** decimals
    return math.floor(number * factor) / factor

# Calculate and print runtime statistics and update timestep
def calc_runtime_stats_timestep(Mpi, u, t, tsp, text_file_handles, h_f_X, Re, time_control): 

    DN, NM = DENO(u, Mpi, h_f_X)
    C_no_real = DN*tsp
    local_Re = NM*float(Re) 

    Mpi.set_barrier()
    if Mpi.get_rank() == 0:
        text_file_handles[1].write(f"{t}    {tsp}     {C_no_real}     {local_Re}\n")

    if time_control.get('variable_timestep') == True:   
        tsp = round_decimals_down(time_control.get('C_no')/DN, 5) 

    return tsp    

# Update solution at end of time loop
def update_variables(u_, p_, T_):

    u_[2].assign(u_[1])
    u_[1].assign(u_[0])
    p_[2].assign(p_[1])    
    p_[1].assign(p_[0])
    
    T_[3].assign(T_[2])
    T_[2].assign(T_[1])
    T_[1].assign(T_[0])



         
        
        



