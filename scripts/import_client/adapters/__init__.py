from scripts.import_client.adapters.athome4_loadfile import Athome4LoadfileAdapter
from scripts.import_client.adapters.base import DatasetAdapter
from scripts.import_client.adapters.edrm_enron_v2 import EdrmEnronV2Adapter
from scripts.import_client.adapters.emc2 import Emc2Adapter
from scripts.import_client.adapters.enron_csv import EnronCsvAdapter
from scripts.import_client.adapters.industrydocuments import (
    IndustryDocumentsLoadfileAdapter,
)
from scripts.import_client.adapters.jeb_bush_inventory import JebBushInventoryAdapter

SanFranciscoWalgreensLoadfileAdapter = IndustryDocumentsLoadfileAdapter

__all__ = [
    "Athome4LoadfileAdapter",
    "DatasetAdapter",
    "EdrmEnronV2Adapter",
    "Emc2Adapter",
    "EnronCsvAdapter",
    "IndustryDocumentsLoadfileAdapter",
    "JebBushInventoryAdapter",
    "SanFranciscoWalgreensLoadfileAdapter",
]
