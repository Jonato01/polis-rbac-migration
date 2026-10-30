import pandas as pd
import csv

def fix_csv_file(input_file, output_file):
    """Fix malformed CSV by properly handling quotes and special characters."""
    try:
        # Try reading with different parameters
        df = pd.read_csv(
            input_file,
            sep=';',
            quoting=csv.QUOTE_MINIMAL,
            escapechar='\\',
            encoding='latin-1',
            on_bad_lines='skip'  # Skip problematic lines
        )

        # Save the cleaned CSV
        df.to_csv(
            output_file,
            sep=';',
            index=False,
            quoting=csv.QUOTE_MINIMAL,
            encoding='latin-1'
        )

        print(f"Fixed CSV saved to: {output_file}")
        return True

    except Exception as e:
        print(f"Error fixing CSV: {e}")
        return False

# Fix your files
fix_csv_file('sal_3_spaziita.csv', 'sal_3_spaziita_fixed.csv')
fix_csv_file('sal_3_sportUnico.csv', 'sal_3_sportUnico_fixed.csv')