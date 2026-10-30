import os
"""
Aggiorna un database a partire da un file Excel con due schede,
usando la colonna 'numero_vap' per individuare le righe da aggiornare.
"""

import argparse
import pandas as pd
from sqlalchemy import create_engine, MetaData, Table


def update_db_from_excel(excel_file: str,
                         db_url: str,
                         table_name: str,
                         sheet_names: list[str] = None,
                         excel_key_col: str = 'Numero VAP',
                         db_key_col: str = 'riferimento_bpm'):
    """
    Legge un file Excel e aggiorna il database.
    """
    if sheet_names is None:
        sheet_names = ['Sheet1', 'Sheet2']

    # Crea l'engine con configurazione SSL per Azure MySQL
    engine = create_engine(
        db_url,
        connect_args={
            "ssl_disabled": False,
            "ssl_verify_cert": False,
            "ssl_verify_identity": False
        },
        pool_timeout=30,
        pool_recycle=3600
    )

    metadata = MetaData()
    table = Table(table_name, metadata, autoload_with=engine)

    for sheet in sheet_names:
        print(f"Elaborazione foglio '{sheet}'...")
        df = pd.read_excel(excel_file, sheet_name=sheet, engine='openpyxl')

        # Verifica colonna chiave Excel
        if excel_key_col not in df.columns:
            print(f"  Attenzione: colonna '{excel_key_col}' non trovata in '{sheet}'. Skip.")
            continue

        # Itera record
        with engine.begin() as conn:
            for _, row in df.iterrows():
                key_value = row[excel_key_col]
                # Prepara i dati da aggiornare
                update_data = {
                    col: row[col]
                    for col in df.columns
                    if col != excel_key_col and pd.notna(row[col])
                }
                if not update_data:
                    continue

                # Costruisci lo statement di update
                stmt = (
                    table.update()
                         .where(getattr(table.c, db_key_col) == key_value)
                         .values(**update_data)
                )
                result = conn.execute(stmt)
                if result.rowcount > 0:
                    print(f"  Aggiornato record con {db_key_col}={key_value}")

    print("Aggiornamento completato.")


if __name__ == '__main__':
    # URL di connessione con parametri SSL per Azure MySQL
    db = os.getenv("DB_SQLALCHEMY_URL", "")

    update_db_from_excel(
        excel_file=r"C:\Users\Renato\OneDrive\Documenti\da caricare\polis\8_SAL_30_04_2025\Riepilogo analitico spese sostenute 8 SAL.xlsx",
        db_url=db,
        table_name="fatture",
        sheet_names=["Report SPORTELLOUNICO", "Report SPAZIITALIA"],
        excel_key_col="Numero VAP",
        db_key_col="riferimento_bpm"
    )