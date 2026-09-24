"""
Erstelle Test-PDF mit PyMuPDF
"""

import fitz  # PyMuPDF

def create_test_pdf():
    """Erstelle eine Test-PDF-Datei mit Rechnungsdaten"""
    doc = fitz.open()
    
    # Seite hinzufügen
    page = doc.new_page(width=595, height=842)  # A4
    
    # Text auf der Seite
    text = """
Rechnung

Rechnungsnummer: R-2024-001
Datum: 15.03.2024
Fälligkeitsdatum: 15.04.2024

Absender:
Telekom Deutschland GmbH
Kaiserstraße 59
53113 Bonn

Empfänger:
Hans Müller
Musterstraße 1
12345 Berlin

Leistungen:
Mobilfunkdienstleistung: 29,99 EUR
Internetflat: 19,99 EUR
TV-Paket: 12,99 EUR

Gesamtbetrag: 62,97 EUR
"""
    
    # Text auf der Seite schreiben
    page.insert_text((50, 50), text, fontsize=12)
    
    # PDF speichern
    doc.save(str(__import__("pathlib").Path.home() / "Documents/Dokumente/Eingang/test_rechnung.pdf"))
    doc.close()
    
    print("Test-PDF erstellt: ~/Documents/Dokumente/Eingang/test_rechnung.pdf")

if __name__ == "__main__":
    create_test_pdf()