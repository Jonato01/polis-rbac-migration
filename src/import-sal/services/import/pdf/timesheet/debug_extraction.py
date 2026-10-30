#!/usr/bin/env python3
"""Debug script to trace extraction logic"""

filename = "TM_POLIS_DI MARCO FRANCESCO_gen_apr25.pdf"

# Pulisce prefissi comuni e estensione
name_part = filename.replace('.pdf', '').replace('TM_POLIS_', '').replace('TM_', '')
print(f"After cleanup: '{name_part}'")

# Normalizza spazi multipli
name_part = ' '.join(name_part.split())
print(f"After normalize: '{name_part}'")

# Separa sia per underscore CHE per spazio per isolare tutte le parti
name_part = name_part.replace('_', ' ')
print(f"After underscore replacement: '{name_part}'")

parts = name_part.split()
print(f"Parts: {parts}")

name_parts = []
months = ['gen', 'feb', 'mar', 'apr', 'mag', 'giu', 'lug', 'ago', 'set', 'ott', 'nov', 'dic']
surname_particles = ['de', 'di', 'del', 'della', 'degli', 'delle', 'da', 'dal', 'dalla', 'van', 'von', 'la', 'le']

i = 0
while i < len(parts):
    part = parts[i].strip()
    print(f"\n--- Processing i={i}, part='{part}' ---")

    if not part:
        print(f"  Empty part, skip")
        i += 1
        continue

    part_lower = part.lower()

    # Se troviamo un mese, STOP
    # Match esatto (gen, mar, etc.) O mese+numeri (gen25, mar24, etc.)
    is_month = (part_lower in months) or any(
        part_lower.startswith(month) and len(part_lower) > len(month) and part_lower[len(month):].isdigit()
        for month in months
    )
    if is_month:
        print(f"  Found month, BREAK")
        break

    # Salta numeri, mix numero-lettere, "polis", parole troppo corte (eccetto particelle)
    is_number = part_lower.isdigit() or any(char.isdigit() for char in part_lower)
    is_too_short = len(part) <= 2 and part_lower not in surname_particles
    is_polis = part_lower in ['polis', '']

    print(f"  is_number={is_number}, is_too_short={is_too_short}, is_polis={is_polis}")

    if is_number or is_too_short or is_polis:
        print(f"  Skip due to filters")
        i += 1
        continue

    # Se è una particella di cognome E c'è una parola successiva valida, uniscile
    if part_lower in surname_particles and i + 1 < len(parts):
        print(f"  Is surname particle, checking next part...")
        next_part = parts[i + 1].strip()
        next_part_lower = next_part.lower()
        print(f"  Next part: '{next_part}'")

        # Verifica che la prossima parte non sia un mese/numero/invalid
        next_is_month = (next_part_lower in months) or any(
            next_part_lower.startswith(month) and len(next_part_lower) > len(month) and next_part_lower[len(month):].isdigit()
            for month in months
        )
        has_digit = any(char.isdigit() for char in next_part_lower)
        is_digit = next_part_lower.isdigit()
        is_long_enough = len(next_part) > 2

        print(f"  next_is_month={next_is_month}, has_digit={has_digit}, is_digit={is_digit}, is_long_enough={is_long_enough}")

        next_is_valid = (
            next_part and
            not next_is_month and
            not is_digit and
            not has_digit and
            is_long_enough
        )

        print(f"  Next is_valid: {next_is_valid}")

        if next_is_valid:
            # Unisci particella + cognome
            combined = f"{part} {next_part}"
            name_parts.append(combined)
            print(f"  Combined '{part}' + '{next_part}' = '{combined}'")
            print(f"  name_parts now: {name_parts}")
            i += 2
            continue

    # Parte valida normale
    print(f"  Adding as normal part: '{part}'")
    name_parts.append(part)
    print(f"  name_parts now: {name_parts}")
    i += 1

print(f"\n=== FINAL ===")
print(f"name_parts: {name_parts}")
result = ' '.join(name_parts).upper()
print(f"Result: '{result}'")

