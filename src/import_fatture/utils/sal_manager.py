# utils/sal_manager.py
from datetime import datetime, date
from dateutil.relativedelta import relativedelta

class SALManager:
    """Gestisce la logica temporale dei SAL"""
    
    # Configurazione base: primo SAL inizia a settembre 2022
    PRIMO_SAL_ANNO = 2022
    PRIMO_SAL_MESE = 9  # Settembre
    MESI_PER_SAL = 4

    # Mapping mesi italiano
    MESI_ITA = {
        1: 'Gennaio', 2: 'Febbraio', 3: 'Marzo', 4: 'Aprile',
        5: 'Maggio', 6: 'Giugno', 7: 'Luglio', 8: 'Agosto',
        9: 'Settembre', 10: 'Ottobre', 11: 'Novembre', 12: 'Dicembre'
    }
    
    # Abbreviazioni mesi
    MESI_ABBR = {
        1: 'gen', 2: 'feb', 3: 'mar', 4: 'apr',
        5: 'mag', 6: 'giu', 7: 'lug', 8: 'ago',
        9: 'set', 10: 'ott', 11: 'nov', 12: 'dic'
    }
    
    
    @classmethod
    def get_periodo_sal(cls, numero_sal):
        """
        Restituisce il periodo (data_inizio, data_fine) per un dato numero SAL
        """
        # Calcola quanti mesi sono passati dal primo SAL
        mesi_dal_primo = (numero_sal - 1) * cls.MESI_PER_SAL
        
        # Data inizio del SAL
        data_inizio = date(cls.PRIMO_SAL_ANNO, cls.PRIMO_SAL_MESE, 1)
        data_inizio += relativedelta(months=mesi_dal_primo)
        
        # Data fine del SAL (ultimo giorno del 4° mese)
        data_fine = data_inizio + relativedelta(months=cls.MESI_PER_SAL, days=-1)
        
        return data_inizio, data_fine
    
    @classmethod
    def get_mesi_sal(cls, numero_sal):
        """
        Restituisce la lista dei mesi (numero, nome) per un dato SAL
        """
        data_inizio, _ = cls.get_periodo_sal(numero_sal)
        
        mesi = []
        for i in range(cls.MESI_PER_SAL):
            data_mese = data_inizio + relativedelta(months=i)
            mesi.append({
                'numero': data_mese.month,
                'nome': cls.MESI_ITA[data_mese.month],
                'nome_breve': cls.MESI_ABBR[data_mese.month],
                'anno': data_mese.year
            })
        
        return mesi
    
    @classmethod
    def get_column_mapping_personale(cls, numero_sal):
        """
        Genera il mapping delle colonne per il personale basato sul SAL
        """
        mesi = cls.get_mesi_sal(numero_sal)
        mapping = {}
        
        # Mapping base sempre presente
        base_mapping = {
            'N°': 'cod_dipendente',
            'COGNOME': 'cognome',
            'NOME': 'nome',
            'SEDE': 'sede',
            'CITTÀ': 'citta',
            'QUALIFICA': 'qualifica',
            'COSTO MEDIO ORARIO': 'costo_medio_orario',
            'DATA PASSAGGIO LIVELLO': 'data_passaggio_livello',
            'NUOVA QUALIFICA': 'nuova_qualifica',
            'COSTO MEDIO ORARIO (post passaggio)': 'costo_medio_orario_post'
        }
        
        # Aggiungi mapping dinamico per ore e costi
        for i, mese in enumerate(mesi):
            # Genera chiavi possibili per le ore
            chiavi_ore = [
                f"ORE {mese['nome_breve'].upper()} {str(mese['anno'])[2:]}",
                f"ORE {mese['nome'].upper()[:3]} {str(mese['anno'])[2:]}",
                f"ORE {mese['nome'].upper()} {str(mese['anno'])[2:]}"
            ]
            
            # Aggiungi al mapping
            for chiave in chiavi_ore:
                mapping[chiave] = f"ore_mese_{i+1}"
            
            # Stessa cosa per i costi
            chiavi_costo = [
                f"COSTO {mese['nome_breve'].upper()} {str(mese['anno'])[2:]}",
                f"COSTO {mese['nome'].upper()[:3]} {str(mese['anno'])[2:]}",
                f"COSTO {mese['nome'].upper()} {str(mese['anno'])[2:]}"
            ]
            
            for chiave in chiavi_costo:
                mapping[chiave] = f"costo_mese_{i+1}"
        
        mapping.update(base_mapping)
        return mapping