from pathlib import Path

import pytest

from autosampler.config import MachineConfig
from autosampler.controllers.mock_grbl import MockGrblController
from autosampler.controllers.mock_scale import MockScaleController
from autosampler.machine import AutosamplerMachine
from autosampler.persistence import StateStore
from autosampler.vessels import Inventory, Vessel, VesselCategory, VesselType


@pytest.fixture
def configured_machine(tmp_path):
    config = MachineConfig(state_file=tmp_path / "state.json", log_file=tmp_path / "test.log")
    config.z.safe_position_mm = 0.0
    config.z.max_travel_mm = 150.0
    config.z.motion_speed_mm_min = 100.0
    config.z.acceleration_mm_s2 = 10.0
    config.z.dispense_position_mm = 10.0
    config.z.aspiration_machine_offset_mm = 5.0
    for pipette in (config.pipette_1, config.pipette_2):
        pipette.contact_position_mm = 1.0
        pipette.max_travel_mm = 20.0
        pipette.volume_per_mm_ul = pipette.max_volume_ul / 10.0
        pipette.speed_mm_min = 100.0
        pipette.acceleration_mm_s2 = 10.0
    config.transfer.default_draw_buffer_percent = 0.0
    config.transfer.tolerance_ul = 1.0
    config.transfer.max_correction_attempts = 3
    config.transfer.scale_stabilization_time_s = 0.0
    config.transfer.minimum_correction_volume_ul = 0.01
    config.transfer.aspiration_immersion_depth_mm = 1.0
    config.transfer.z_liquid_clearance_mm = 1.0
    positions = {"source": 0.0, "dest": 30.0, "waste": 60.0, "clean": 90.0}
    config.pipette_1_positions_deg.update(positions)
    config.pipette_2_positions_deg.update(positions)
    inventory = Inventory([
        Vessel("source", 1, "Source", VesselCategory.INPUT, VesselType.VIAL_15ML, 10_000.0),
        Vessel("dest", 2, "Destination", VesselCategory.OUTPUT, VesselType.VIAL_15ML, 0.0),
        Vessel("waste", 25, "Waste", VesselCategory.WASTE, VesselType.BEAKER_100ML, 0.0),
        Vessel("clean", 26, "Cleaning", VesselCategory.CLEANING, VesselType.BEAKER_100ML, 50_000.0),
    ])
    grbl, scale = MockGrblController(), MockScaleController()
    machine = AutosamplerMachine(config, grbl, scale, inventory, StateStore(config.state_file))
    machine.connect_grbl()
    machine.connect_scale()
    machine.home()
    return machine
