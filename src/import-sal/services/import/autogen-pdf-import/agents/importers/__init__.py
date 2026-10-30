"""
Importers package per AutoGen PDF Import

SISTEMA COMPLETAMENTE DINAMICO:
- I tipi documento sono configurati nel DATABASE (tabella autogen_document_types)
- I campi vengono scoperti automaticamente via DESCRIBE della tabella
- Il prompt di estrazione e la query SQL vengono generati automaticamente

Per aggiungere un nuovo tipo documento:
1. Creare la tabella destinazione nel DB (con commenti sulle colonne)
2. Inserire record in autogen_document_types
3. NESSUNA modifica al codice Python necessaria!
"""
from agents.importers.dynamic_importer import (
    DynamicImporter,
    get_importer,
    get_all_importers,
    clear_importer_cache
)

__all__ = [
    'DynamicImporter',
    'get_importer',
    'get_all_importers',
    'clear_importer_cache',
]