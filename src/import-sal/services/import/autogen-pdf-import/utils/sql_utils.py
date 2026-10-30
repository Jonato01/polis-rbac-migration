"""
SQL Utilities
Helper per costruzione query SQL safe
"""
import re
from typing import Any
from datetime import date, datetime


def safe_value(value: Any, max_length: int = None) -> str:
    """
    Escape e sanitizza un valore per SQL

    Args:
        value: Valore da sanitizzare
        max_length: Lunghezza massima (opzionale)

    Returns:
        Stringa safe per SQL (con quotes se stringa)
    """
    if value is None:
        return "NULL"

    if isinstance(value, bool):
        return "1" if value else "0"

    if isinstance(value, (int, float)):
        return str(value)

    if isinstance(value, (date, datetime)):
        return f"'{value.strftime('%Y-%m-%d')}'"

    # Converti a stringa
    str_value = str(value)

    # Tronca se necessario
    if max_length and len(str_value) > max_length:
        str_value = str_value[:max_length]

    # Escape caratteri speciali
    str_value = str_value.replace("\\", "\\\\")
    str_value = str_value.replace("'", "\\'")
    str_value = str_value.replace('"', '\\"')
    str_value = str_value.replace("\n", "\\n")
    str_value = str_value.replace("\r", "\\r")
    str_value = str_value.replace("\t", "\\t")
    str_value = str_value.replace("\x00", "")  # Remove null bytes

    return f"'{str_value}'"


def safe_number(value: Any, default: float = 0) -> str:
    """
    Converte un valore in numero safe per SQL

    Args:
        value: Valore da convertire
        default: Valore default se conversione fallisce

    Returns:
        Numero come stringa
    """
    if value is None:
        return str(default)

    try:
        # Gestisci formato italiano (1.234,56 -> 1234.56)
        str_val = str(value).strip()

        # Rimuovi simboli valuta
        str_val = re.sub(r'[€$£]', '', str_val).strip()

        # Se contiene sia . che , determina il formato
        if ',' in str_val and '.' in str_val:
            # Formato italiano: 1.234,56
            if str_val.rfind(',') > str_val.rfind('.'):
                str_val = str_val.replace('.', '').replace(',', '.')
            # Formato inglese: 1,234.56
            else:
                str_val = str_val.replace(',', '')
        elif ',' in str_val:
            # Solo virgola, potrebbe essere decimale italiano
            str_val = str_val.replace(',', '.')

        # Rimuovi spazi
        str_val = str_val.replace(' ', '')

        return str(float(str_val))

    except (ValueError, TypeError):
        return str(default)


def safe_date(value: Any) -> str:
    """
    Converte un valore in data SQL (YYYY-MM-DD)

    Args:
        value: Valore da convertire (stringa, date, datetime)

    Returns:
        Data formattata o NULL
    """
    if value is None:
        return "NULL"

    if isinstance(value, datetime):
        return f"'{value.strftime('%Y-%m-%d')}'"

    if isinstance(value, date):
        return f"'{value.strftime('%Y-%m-%d')}'"

    str_val = str(value).strip()

    if not str_val:
        return "NULL"

    # Prova vari formati
    formats = [
        '%Y-%m-%d',      # 2025-01-21
        '%d/%m/%Y',      # 21/01/2025
        '%d-%m-%Y',      # 21-01-2025
        '%d.%m.%Y',      # 21.01.2025
        '%Y%m%d',        # 20250121
    ]

    for fmt in formats:
        try:
            parsed = datetime.strptime(str_val, fmt)
            return f"'{parsed.strftime('%Y-%m-%d')}'"
        except ValueError:
            continue

    return "NULL"


def safe_enum(value: Any, allowed_values: list[str], default: str = None) -> str:
    """
    Converte un valore in un ENUM safe per SQL

    Args:
        value: Valore da convertire
        allowed_values: Lista di valori permessi per l'ENUM
        default: Valore default se non trovato match

    Returns:
        Valore ENUM safe per SQL

    Examples:
        >>> safe_enum('yes', ['SI', 'NO'])  # -> 'SI'
        >>> safe_enum(True, ['SI', 'NO'])   # -> 'SI'
        >>> safe_enum('NO', ['SI', 'NO'])   # -> 'NO'
    """
    if value is None:
        return f"'{default}'" if default else "NULL"

    # Normalizza il valore in stringa uppercase
    str_val = str(value).strip().upper()

    # Mapping comuni per boolean-like values
    true_values = ['SI', 'SÌ', 'YES', 'TRUE', '1', 'Y', 'S']
    false_values = ['NO', 'FALSE', '0', 'N']

    # Se il valore è già nella lista allowed, usalo direttamente
    allowed_upper = [v.upper() for v in allowed_values]
    if str_val in allowed_upper:
        # Trova il valore originale con il case corretto
        idx = allowed_upper.index(str_val)
        return f"'{allowed_values[idx]}'"

    # Prova mapping boolean-like
    if str_val in true_values:
        # Cerca 'SI' o simili negli allowed values
        for val in allowed_values:
            if val.upper() in ['SI', 'SÌ', 'YES', 'Y', 'S']:
                return f"'{val}'"

    if str_val in false_values:
        # Cerca 'NO' o simili negli allowed values
        for val in allowed_values:
            if val.upper() in ['NO', 'N']:
                return f"'{val}'"

    # Se c'è un default, usalo
    if default and default in allowed_values:
        return f"'{default}'"

    # Ultimo tentativo: primo valore allowed
    if allowed_values:
        return f"'{allowed_values[0]}'"

    return "NULL"


def build_insert_query(
    table: str,
    data: dict,
    ignore_fields: list[str] = None,
    on_duplicate_update: list[str] = None
) -> str:
    """
    Costruisce una query INSERT

    Args:
        table: Nome tabella
        data: Dictionary con colonna -> valore
        ignore_fields: Campi da ignorare
        on_duplicate_update: Campi da aggiornare su duplicate key

    Returns:
        Query SQL INSERT
    """
    ignore_fields = ignore_fields or []

    # Filtra campi
    fields = {k: v for k, v in data.items() if k not in ignore_fields}

    columns = list(fields.keys())
    values = [safe_value(v) for v in fields.values()]

    query = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(values)})"

    if on_duplicate_update:
        updates = [f"{col} = VALUES({col})" for col in on_duplicate_update if col in columns]
        if updates:
            query += f" ON DUPLICATE KEY UPDATE {', '.join(updates)}"

    return query


def build_upsert_query(
    table: str,
    data: dict,
    key_fields: list[str],
    ignore_fields: list[str] = None
) -> str:
    """
    Costruisce una query INSERT ... ON DUPLICATE KEY UPDATE

    Args:
        table: Nome tabella
        data: Dictionary con colonna -> valore
        key_fields: Campi chiave (non aggiornati su duplicate)
        ignore_fields: Campi da ignorare completamente

    Returns:
        Query SQL INSERT ... ON DUPLICATE KEY UPDATE
    """
    ignore_fields = ignore_fields or []

    # Filtra campi
    fields = {k: v for k, v in data.items() if k not in ignore_fields}

    columns = list(fields.keys())
    values = [safe_value(v) for v in fields.values()]

    # Campi da aggiornare (escludi chiavi)
    update_fields = [col for col in columns if col not in key_fields]
    updates = [f"{col} = VALUES({col})" for col in update_fields]

    query = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(values)})"

    if updates:
        query += f" ON DUPLICATE KEY UPDATE {', '.join(updates)}"

    return query

