"""modelx-bridge -- the kernel side of lifelib Studio.

A transport-agnostic dispatcher over modelx, plus a JSON value codec and a
Jupyter comm adapter. The wire protocol is docs/bridge-protocol-v0.md.

    from modelx_bridge import Bridge
    bridge = Bridge()
    bridge.dispatch("model.open_sample", {"sample": "termlife"})

In a Jupyter kernel, `modelx_bridge.jupyter.register_comm()` puts the same
dispatcher behind the `modelx-bridge` comm target.
"""

from . import files, jupyter, samples, tables
from .codec import Codec
from .errors import BridgeError
from .files import (backup_default, backup_limit, backup_slots, list_dir,
                    set_storage_root, storage_info, storage_probe,
                    storage_root)
from .jupyter import TARGET, register_comm
from .methods import (FEATURES, KERNEL_ID, PROTOCOL, VERSION, Bridge,
                      kernel_block, split_buffers)
from .samples import (boot, boot_report, build_sample, find_model_dir,
                      opened_samples, register_alias, register_sample,
                      sample_catalog, sample_ids, sample_of, sample_paths,
                      saved_models, site_base_url, stage_model)

__version__ = VERSION

__all__ = ["Bridge", "BridgeError", "Codec", "FEATURES", "KERNEL_ID",
           "PROTOCOL", "TARGET", "VERSION", "backup_default", "backup_limit",
           "backup_slots", "boot", "boot_report", "build_sample", "files",
           "find_model_dir", "jupyter", "kernel_block", "list_dir",
           "opened_samples", "register_alias", "register_comm", "register_sample",
           "sample_catalog", "sample_ids", "sample_of", "sample_paths",
           "samples", "saved_models", "set_storage_root", "site_base_url",
           "split_buffers", "stage_model", "storage_info", "storage_probe",
           "storage_root", "tables"]
