from inspect import signature
from typing import Optional
from enum import Flag, auto
import numpy as np

from pdb import set_trace


class WPFModelType(Flag):
    """
    Flags to signify capabilities and other semantics of a Model
    """

    BACKGROUND = auto()

    AMORPHOUS = auto()
    LATTICE = auto()

    DUMMY = auto()  # Model has no direct contribution to pattern
    META = auto()  # Model depends on multiple sub-Models


class WPFModelPrototype:
    """
    Prototype class for a compent of a whole-pattern model.
    Holds the following:
        name:       human-readable name of the model
        params:     a dict of names and initial (or returned) values of the model parameters
        func:       a function that takes as arguments:
                        • the diffraction pattern being built up, which the function should modify in place
                        • positional arguments in the same order as the params dictionary
                        • keyword arguments. this is to provide some pre-computed information for convenience
                            kwargs will include:
                                • xArray, yArray    meshgrid of the x and y coordinates
                                • global_x0         global x-coordinate of the pattern center
                                • global_y0         global y-coordinate of the pattern center
        jacobian:   a function that takes as arguments:
                        • the diffraction pattern being built up, which the function should modify in place
                        • positional arguments in the same order as the params dictionary
                        • offset: the first index (j) that values should be written into
                            (the function should ONLY write into 0,1, and offset:offset+nParams)
                            0 and 1 are the entries for global_x0 and global_y0, respectively
                            **REMEMBER TO ADD TO 0 and 1 SINCE ALL MODELS CAN CONTRIBUTE TO THIS PARTIAL DERIVATIVE**
                        • keyword arguments. this is to provide some pre-computed information for convenience
    """

    def __init__(self, name: str, params: dict, model_type=WPFModelType.DUMMY):
        self.name = name
        self.params = params

        self.nParams = len(params.keys())

        self.hasJacobian = getattr(self, "jacobian", None) is not None

        self.model_type = model_type

    def func(self, DP: np.ndarray, x, **kwargs) -> None:
        raise NotImplementedError()

    # Required signature for the Jacobian:
    #
    # def jacobian(self, J: np.ndarray, *args, offset: int, **kwargs) -> None:
    #     raise NotImplementedError()


class Parameter:
    def __init__(
        self,
        initial_value,
        lower_bound: Optional[float] = None,
        upper_bound: Optional[float] = None,
    ):
        if hasattr(initial_value, "__iter__"):
            if len(initial_value) == 2:
                initial_value = (
                    initial_value[0],
                    initial_value[0] - initial_value[1],
                    initial_value[0] + initial_value[1],
                )
            self.set_params(*initial_value)
        else:
            self.set_params(initial_value, lower_bound, upper_bound)

        # Store a dummy offset. This must be set by WPF during setup
        # This stores the index in the master parameter and Jacobian arrays
        # corresponding to this parameter
        self.offset = np.nan

    def set_params(
        self,
        initial_value,
        lower_bound,
        upper_bound,
    ):
        self.initial_value = initial_value
        self.lower_bound = lower_bound if lower_bound is not None else -np.inf
        self.upper_bound = upper_bound if upper_bound is not None else np.inf

    def __str__(self):
        return f"Value: {self.initial_value} (Range: {self.lower_bound},{self.upper_bound})"

    def __repr__(self):
        return f"Value: {self.initial_value} (Range: {self.lower_bound},{self.upper_bound})"


class _BaseModel(WPFModelPrototype):
    """
    Model object used by the WPF class as a container for the global Parameters
    """

    def __init__(self, x0, y0, name="Globals"):
        params = {"x center": Parameter(x0), "y center": Parameter(y0)}

        super().__init__(name, params, model_type=WPFModelType.DUMMY)

    def func(self, DP: np.ndarray, x, **kwargs) -> None:
        pass

    def jacobian(self, J: np.ndarray, *args, **kwargs) -> None:
        pass


class DCBackground(WPFModelPrototype):
    def __init__(self, background_value=0.0, name="DC Background"):
        params = {"DC Level": Parameter(background_value)}

        super().__init__(name, params, model_type=WPFModelType.BACKGROUND)

    def func(self, DP: np.ndarray, x, **kwargs) -> None:
        DP += x[self.params["DC Level"].offset]

    def jacobian(self, J: np.ndarray, *args, **kwargs):
        J[:, self.params["DC Level"].offset] = 1


class GaussianBackground(WPFModelPrototype):
    def __init__(
        self,
        WPF,
        sigma,
        intensity,
        global_center=True,
        x0=0.0,
        y0=0.0,
        name="Gaussian Background",
    ):
        params = {"sigma": Parameter(sigma), "intensity": Parameter(intensity)}
        if global_center:
            params["x center"] = WPF.coordinate_model.params["x center"]
            params["y center"] = WPF.coordinate_model.params["y center"]
        else:
            params["x center"] = Parameter(x0)
            params["y center"] = Parameter(y0)

        super().__init__(name, params, model_type=WPFModelType.BACKGROUND)

    def func(self, DP: np.ndarray, x: np.ndarray, **kwargs) -> None:
        sigma = x[self.params["sigma"].offset]
        level = x[self.params["intensity"].offset]

        r = kwargs["parent"]._get_distance(
            x, self.params["x center"], self.params["y center"]
        )

        DP += level * np.exp(r**2 / (-2 * sigma**2))

    def jacobian(self, J: np.ndarray, x: np.ndarray, **kwargs) -> None:
        sigma = x[self.params["sigma"].offset]
        level = x[self.params["intensity"].offset]
        x0 = x[self.params["x center"].offset]
        y0 = x[self.params["y center"].offset]

        r = kwargs["parent"]._get_distance(
            x, self.params["x center"], self.params["y center"]
        )
        exp_expr = np.exp(r**2 / (-2 * sigma**2))

        # dF/d(x0)
        J[:, self.params["x center"].offset] += (
            level * (kwargs["xArray"] - x0) * exp_expr / sigma**2
        ).ravel()

        # dF/d(y0)
        J[:, self.params["y center"].offset] += (
            level * (kwargs["yArray"] - y0) * exp_expr / sigma**2
        ).ravel()

        # dF/s(sigma)
        J[:, self.params["sigma"].offset] += (
            level * r**2 * exp_expr / sigma**3
        ).ravel()

        # dF/d(level)
        J[:, self.params["intensity"].offset] += exp_expr.ravel()


class GaussianRing(WPFModelPrototype):
    def __init__(
        self,
        WPF,
        radius,
        sigma,
        intensity,
        global_center=True,
        x0=0.0,
        y0=0.0,
        name="Gaussian Ring",
    ):
        params = {
            "radius": Parameter(radius),
            "sigma": Parameter(sigma),
            "intensity": Parameter(intensity),
            "x center": WPF.coordinate_model.params["x center"]
            if global_center
            else Parameter(x0),
            "y center": WPF.coordinate_model.params["y center"]
            if global_center
            else Parameter(y0),
        }

        super().__init__(name, params, model_type=WPFModelType.AMORPHOUS)

    def global_center_func(self, DP: np.ndarray, x: np.ndarray, **kwargs) -> None:
        radius = x[self.params["radius"].offset]
        sigma = x[self.params["sigma"].offset]
        level = x[self.params["level"].offset]

        r = WPF._get_distance(x, self.params["x center"], self.params["y center"])

        DP += level * np.exp((r - radius) ** 2 / (-2 * sigma**2))

    def global_center_jacobian(self, J: np.ndarray, x: np.ndarray, **kwargs) -> None:
        radius = x[self.params["radius"].offset]
        sigma = x[self.params["sigma"].offset]
        level = x[self.params["level"].offset]

        x0 = x[self.params["x center"].offset]
        y0 = x[self.params["y center"].offset]
        r = WPF._get_distance(x, self.params["x center"], self.params["y center"])

        local_r = radius - r
        clipped_r = np.maximum(local_r, 0.1)

        exp_expr = np.exp(local_r**2 / (-2 * sigma**2))

        # dF/d(x0)
        J[:, self.params["x center"].offset] += (
            level
            * exp_expr
            * (kwargs["xArray"] - x0)
            * local_r
            / (sigma**2 * clipped_r)
        ).ravel()

        # dF/d(y0)
        J[:, self.parans["y center"].offset] += (
            level
            * exp_expr
            * (kwargs["yArray"] - x0)
            * local_r
            / (sigma**2 * clipped_r)
        ).ravel()

        # dF/d(radius)
        J[:, self.params["radius"].offset] += (
            -1.0 * level * exp_expr * local_r / (sigma**2)
        ).ravel()

        # dF/d(sigma)
        J[:, self.params["sigma"].offset] += (
            level * local_r**2 * exp_expr / sigma**3
        ).ravel()

        # dF/d(intensity)
        J[:, self.params["intensity"].offset] += exp_expr.ravel()


class SyntheticDiskLattice(WPFModelPrototype):
    def __init__(
        self,
        WPF,
        ux: float,
        uy: float,
        vx: float,
        vy: float,
        disk_radius: float,
        disk_width: float,
        u_max: int,
        v_max: int,
        intensity_0: float,
        refine_radius: bool = False,
        refine_width: bool = False,
        global_center: bool = True,
        x0: float = 0.0,
        y0: float = 0.0,
        exclude_indices: list = [],
        include_indices: list = None,
        name="Synthetic Disk Lattice",
        verbose=False,
    ):
        self.disk_radius = disk_radius
        self.disk_width = disk_width

        params = {}

        if global_center:
            params["x center"] = WPF.coordinate_model.params["x center"]
            params["y center"] = WPF.coordinate_model.params["y center"]
        else:
            params["x center"] = Parameter(x0)
            params["y center"] = Parameter(y0)

        x0 = params["x center"].initial_value
        y0 = params["y center"].initial_value

        params["ux"] = Parameter(ux)
        params["uy"] = Parameter(uy)
        params["vx"] = Parameter(vx)
        params["vy"] = Parameter(vy)

        Q_Nx = WPF.static_data["Q_Nx"]
        Q_Ny = WPF.static_data["Q_Ny"]

        if include_indices is None:
            u_inds, v_inds = np.mgrid[-u_max : u_max + 1, -v_max : v_max + 1]
            self.u_inds = u_inds.ravel()
            self.v_inds = v_inds.ravel()

            delete_mask = np.zeros_like(self.u_inds, dtype=bool)
            for i, (u, v) in enumerate(zip(u_inds.ravel(), v_inds.ravel())):
                x = (
                    x0
                    + (u * params["ux"].initial_value)
                    + (v * params["vx"].initial_value)
                )
                y = (
                    y0
                    + (u * params["uy"].initial_value)
                    + (v * params["vy"].initial_value)
                )
                if [u, v] in exclude_indices:
                    delete_mask[i] = True
                elif (x < 0) or (x > Q_Nx) or (y < 0) or (y > Q_Ny):
                    delete_mask[i] = True
                    if verbose:
                        print(
                            f"Excluding peak [{u},{v}] because it is outside the pattern..."
                        )
                else:
                    params[f"[{u},{v}] Intensity"] = Parameter(intensity_0)

            self.u_inds = self.u_inds[~delete_mask]
            self.v_inds = self.v_inds[~delete_mask]
        else:
            for ind in include_indices:
                params[f"[{ind[0]},{ind[1]}] Intensity"] = Parameter(intensity_0)
            inds = np.array(include_indices)
            self.u_inds = inds[:, 0]
            self.v_inds = inds[:, 1]

        self.refine_radius = refine_radius
        self.refine_width = refine_width
        if refine_radius:
            params["disk radius"] = Parameter(disk_radius)
        if refine_width:
            params["edge width"] = Parameter(disk_width)

        super().__init__(name, params, model_type=WPFModelType.LATTICE)

    def func(self, DP: np.ndarray, x: np.ndarray, **static_data) -> None:
        x0 = x[self.params["x center"].offset]
        y0 = x[self.params["y center"].offset]
        ux = x[self.params["ux"].offset]
        uy = x[self.params["uy"].offset]
        vx = x[self.params["vx"].offset]
        vy = x[self.params["vy"].offset]

        disk_radius = (
            x[self.params["disk radius"].offset]
            if self.refine_radius
            else self.disk_radius
        )

        disk_width = (
            x[self.params["edge width"].offset]
            if self.refine_width
            else self.disk_width
        )

        for i, (u, v) in enumerate(zip(self.u_inds, self.v_inds)):
            x_pos = x0 + (u * ux) + (v * vx)
            y_pos = y0 + (u * uy) + (v * vy)

            DP += x[self.params[f"[{u},{v}] Intensity"].offset] / (
                1.0
                + np.exp(
                    np.minimum(
                        4
                        * (
                            np.sqrt(
                                (static_data["xArray"] - x_pos) ** 2
                                + (static_data["yArray"] - y_pos) ** 2
                            )
                            - disk_radius
                        )
                        / disk_width,
                        20,
                    )
                )
            )

    def jacobian(self, J: np.ndarray, x: np.ndarray, **static_data) -> None:
        x0 = x[self.params["x center"].offset]
        y0 = x[self.params["y center"].offset]
        ux = x[self.params["ux"].offset]
        uy = x[self.params["uy"].offset]
        vx = x[self.params["vx"].offset]
        vy = x[self.params["vy"].offset]
        WPF = static_data["parent"]

        r = np.maximum(
            5e-1, WPF._get_distance(x, self.params["x center"], self.params["y center"])
        )

        disk_radius = (
            x[self.params["disk radius"].offset]
            if self.refine_radius
            else self.disk_radius
        )

        disk_width = (
            x[self.params["edge width"].offset]
            if self.refine_width
            else self.disk_width
        )

        for i, (u, v) in enumerate(zip(self.u_inds, self.v_inds)):
            x_pos = x0 + (u * ux) + (v * vx)
            y_pos = y0 + (u * uy) + (v * vy)

            disk_intensity = x[self.params[f"[{u},{v}] Intensity"].offset]

            r_disk = np.maximum(
                5e-1,
                np.sqrt(
                    (static_data["xArray"] - x_pos) ** 2
                    + (static_data["yArray"] - y_pos) ** 2
                ),
            )

            mask = r_disk < (2 * disk_radius)

            top_exp = mask * np.exp(4 * ((mask * r_disk) - disk_radius) / disk_width)

            # dF/d(x0)
            dx = (
                4
                * disk_intensity
                * (static_data["xArray"] - x_pos)
                * top_exp
                / ((1.0 + top_exp) ** 2 * disk_width * r)
            ).ravel()

            # dF/d(y0)
            dy = (
                4
                * disk_intensity
                * (static_data["yArray"] - y_pos)
                * top_exp
                / ((1.0 + top_exp) ** 2 * disk_width * r)
            ).ravel()

            # insert center position derivatives
            J[:, self.params["x center"].offset] += disk_intensity * dx
            J[:, self.params["y center"].offset] += disk_intensity * dy

            # insert lattice vector derivatives
            J[:, self.params["ux"].offset] += disk_intensity * u * dx
            J[:, self.params["uy"].offset] += disk_intensity * u * dy
            J[:, self.params["vx"].offset] += disk_intensity * v * dx
            J[:, self.params["vy"].offset] += disk_intensity * v * dy

            # insert intensity derivative
            dI = (mask * (1.0 / (1.0 + top_exp))).ravel()
            J[:, self.params[f"[{u},{v}] Intensity"].offset] += dI

            # insert disk radius derivative
            if self.refine_radius:
                dR = (
                    4.0 * disk_intensity * top_exp / (disk_width * (1.0 + top_exp) ** 2)
                ).ravel()
                J[:, self.params["disk radius"].offset] += dR

            if self.refine_width:
                dW = (
                    4.0
                    * disk_intensity
                    * top_exp
                    * (r_disk - disk_radius)
                    / (disk_width**2 * (1.0 + top_exp) ** 2)
                ).ravel()
                J[:, self.params["edge width"].offset] += dW


class SyntheticDiskMoire(WPFModelPrototype):
    """
    Add Moire peaks arising from two SyntheticDiskLattice lattices.
    The positions of the Moire peaks are derived from the lattice
    vectors of the parent lattices. This model object adds only the intensity of
    each Moire peak as parameters, all other attributes are inherited from the parents
    """

    def __init__(
        self,
        lattice_a: SyntheticDiskLattice,
        lattice_b: SyntheticDiskLattice,
        decorated_peaks: list = None,
        name: str = "Moire Lattice",
    ):
        """
        Parameters
        ----------

        lattice_a, lattice_b: SyntheticDiskLattice
        """
        # ensure both models share the same center coordinate

        # pick the right pair of lattice vectors for generating the moire reciprocal lattice

        #

        super().__init__(
            name,
            params,
            model_type=WPFModelType.META,
        )

    def func(self, DP, x, **static_data):
        pass

    def jacobian(self, J, x, **static_data):
        pass


class ComplexOverlapKernelDiskLattice(WPFModelPrototype):
    def __init__(
        self,
        WPF,
        probe_kernel: np.ndarray,
        ux: float,
        uy: float,
        vx: float,
        vy: float,
        u_max: int,
        v_max: int,
        intensity_0: float,
        exclude_indices: list = [],
        name="Complex Overlapped Disk Lattice",
        verbose=False,
    ):
        params = {}

        # if global_center:
        #     self.func = self.global_center_func
        #     self.jacobian = self.global_center_jacobian

        #     x0 = WPF.static_data["global_x0"]
        #     y0 = WPF.static_data["global_y0"]
        # else:
        #     params["x center"] = Parameter(x0)
        #     params["y center"] = Parameter(y0)
        #     self.func = self.local_center_func

        self.probe_kernelFT = np.fft.fft2(probe_kernel)

        params["ux"] = Parameter(ux)
        params["uy"] = Parameter(uy)
        params["vx"] = Parameter(vx)
        params["vy"] = Parameter(vy)

        u_inds, v_inds = np.mgrid[-u_max : u_max + 1, -v_max : v_max + 1]
        self.u_inds = u_inds.ravel()
        self.v_inds = v_inds.ravel()

        delete_mask = np.zeros_like(self.u_inds, dtype=bool)
        Q_Nx = WPF.static_data["Q_Nx"]
        Q_Ny = WPF.static_data["Q_Ny"]

        self.yqArray = np.tile(np.fft.fftfreq(Q_Ny)[np.newaxis, :], (Q_Nx, 1))
        self.xqArray = np.tile(np.fft.fftfreq(Q_Nx)[:, np.newaxis], (1, Q_Ny))

        for i, (u, v) in enumerate(zip(u_inds.ravel(), v_inds.ravel())):
            x = (
                WPF.static_data["global_x0"]
                + (u * params["ux"].initial_value)
                + (v * params["vx"].initial_value)
            )
            y = (
                WPF.static_data["global_y0"]
                + (u * params["uy"].initial_value)
                + (v * params["vy"].initial_value)
            )
            if [u, v] in exclude_indices:
                delete_mask[i] = True
            elif (x < 0) or (x > Q_Nx) or (y < 0) or (y > Q_Ny):
                delete_mask[i] = True
                if verbose:
                    print(
                        f"Excluding peak [{u},{v}] because it is outside the pattern..."
                    )
            else:
                params[f"[{u},{v}] Intensity"] = Parameter(intensity_0)
                if u == 0 and v == 0:
                    params[f"[{u}, {v}] Phase"] = Parameter(
                        0.0, 0.0, 0.0
                    )  # direct beam clamped at zero phase
                else:
                    params[f"[{u}, {v}] Phase"] = Parameter(0.01, -np.pi, np.pi)

        self.u_inds = self.u_inds[~delete_mask]
        self.v_inds = self.v_inds[~delete_mask]

        self.func = self.global_center_func

        super().__init__(name, params, model_type=WPFModelType.LATTICE)

    def global_center_func(self, DP: np.ndarray, *args, **kwargs) -> None:
        # copy the global centers in the right place for the local center generator
        self.local_center_func(
            DP, kwargs["global_x0"], kwargs["global_y0"], *args, **kwargs
        )

    def local_center_func(self, DP: np.ndarray, *args, **kwargs) -> None:
        x0 = args[0]
        y0 = args[1]
        ux = args[2]
        uy = args[3]
        vx = args[4]
        vy = args[5]

        localDP = np.zeros_like(DP, dtype=np.complex64)

        for i, (u, v) in enumerate(zip(self.u_inds, self.v_inds)):
            x = x0 + (u * ux) + (v * vx)
            y = y0 + (u * uy) + (v * vy)

            localDP += (
                args[2 * i + 6]
                * np.exp(1j * args[2 * i + 7])
                * np.abs(
                    np.fft.ifft2(
                        self.probe_kernelFT
                        * np.exp(-2j * np.pi * (self.xqArray * x + self.yqArray * y))
                    )
                )
            )

        DP += np.abs(localDP) ** 2


class KernelDiskLattice(WPFModelPrototype):
    def __init__(
        self,
        WPF,
        probe_kernel: np.ndarray,
        ux: float,
        uy: float,
        vx: float,
        vy: float,
        u_max: int,
        v_max: int,
        intensity_0: float,
        exclude_indices: list = [],
        name="Custom Kernel Disk Lattice",
        verbose=False,
    ):
        params = {}

        # if global_center:
        #     self.func = self.global_center_func
        #     self.jacobian = self.global_center_jacobian

        #     x0 = WPF.static_data["global_x0"]
        #     y0 = WPF.static_data["global_y0"]
        # else:
        #     params["x center"] = Parameter(x0)
        #     params["y center"] = Parameter(y0)
        #     self.func = self.local_center_func

        self.probe_kernelFT = np.fft.fft2(probe_kernel)

        params["ux"] = Parameter(ux)
        params["uy"] = Parameter(uy)
        params["vx"] = Parameter(vx)
        params["vy"] = Parameter(vy)

        u_inds, v_inds = np.mgrid[-u_max : u_max + 1, -v_max : v_max + 1]
        self.u_inds = u_inds.ravel()
        self.v_inds = v_inds.ravel()

        delete_mask = np.zeros_like(self.u_inds, dtype=bool)
        Q_Nx = WPF.static_data["Q_Nx"]
        Q_Ny = WPF.static_data["Q_Ny"]

        self.yqArray = np.tile(np.fft.fftfreq(Q_Ny)[np.newaxis, :], (Q_Nx, 1))
        self.xqArray = np.tile(np.fft.fftfreq(Q_Nx)[:, np.newaxis], (1, Q_Ny))

        for i, (u, v) in enumerate(zip(u_inds.ravel(), v_inds.ravel())):
            x = (
                WPF.static_data["global_x0"]
                + (u * params["ux"].initial_value)
                + (v * params["vx"].initial_value)
            )
            y = (
                WPF.static_data["global_y0"]
                + (u * params["uy"].initial_value)
                + (v * params["vy"].initial_value)
            )
            if [u, v] in exclude_indices:
                delete_mask[i] = True
            elif (x < 0) or (x > Q_Nx) or (y < 0) or (y > Q_Ny):
                delete_mask[i] = True
                if verbose:
                    print(
                        f"Excluding peak [{u},{v}] because it is outside the pattern..."
                    )
            else:
                params[f"[{u},{v}] Intensity"] = Parameter(intensity_0)

        self.u_inds = self.u_inds[~delete_mask]
        self.v_inds = self.v_inds[~delete_mask]

        self.func = self.global_center_func

        super().__init__(name, params, model_type=WPFModelType.LATTICE)

    def global_center_func(self, DP: np.ndarray, *args, **kwargs) -> None:
        # copy the global centers in the right place for the local center generator
        self.local_center_func(
            DP, kwargs["global_x0"], kwargs["global_y0"], *args, **kwargs
        )

    def local_center_func(self, DP: np.ndarray, *args, **kwargs) -> None:
        x0 = args[0]
        y0 = args[1]
        ux = args[2]
        uy = args[3]
        vx = args[4]
        vy = args[5]

        for i, (u, v) in enumerate(zip(self.u_inds, self.v_inds)):
            x = x0 + (u * ux) + (v * vx)
            y = y0 + (u * uy) + (v * vy)

            DP += (
                args[i + 6]
                * np.abs(
                    np.fft.ifft2(
                        self.probe_kernelFT
                        * np.exp(-2j * np.pi * (self.xqArray * x + self.yqArray * y))
                    )
                )
            ) ** 2
