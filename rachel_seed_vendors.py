"""
Rachel - Vendor Database Seeder
Run once to populate the vendor database from property directory and WO history.

Run with:
  python3 rachel_seed_vendors.py
"""

import sqlite3, os
from datetime import datetime

DB_PATH = os.getenv("RACHEL_DB_PATH", "/tmp/rachel_data.db")

VENDORS = [
    {"name": "Solomon Guebarra", "phone": "310-382-6938", "trade": "general",
     "notes": "Super/Handyman for HOA, helps in units too",
     "properties": ["1040 W 4th street #402 Santa Monica, CA"]},
    {"name": "Palisades Electric", "phone": "310-454-6994", "trade": "electrical",
     "properties": ["1040 W 4th street #402 Santa Monica, CA"]},
    {"name": "Flip Ventura", "phone": "310-629-5914", "trade": "electrical",
     "properties": ["1040 W 4th street #402 Santa Monica, CA"]},
    {"name": "Arunas", "phone": "213-590-5156", "trade": "general",
     "properties": ["1040 W 4th street #402 Santa Monica, CA"]},
    {"name": "Bill Perez", "phone": "323-620-5146", "trade": "general",
     "notes": "Great handyman, very reliable",
     "properties": ["1040 W 4th street #402 Santa Monica, CA",
                    "33 Sea Colony Dr Santa Monica, CA 90405"]},
    {"name": "Henry Pitman", "phone": "310-837-8852", "trade": "plumbing",
     "properties": ["1040 W 4th street #402 Santa Monica, CA",
                    "8921 Wonderland Park Ave Los Angeles, CA 90046"]},
    {"name": "Home Upgrade", "phone": "833-446-6387", "trade": "hvac",
     "properties": ["1040 W 4th street #402 Santa Monica, CA"]},
    {"name": "Brody Pennell", "phone": "310-836-0606", "trade": "hvac",
     "properties": ["1040 W 4th street #402 Santa Monica, CA"]},
    {"name": "Gary Plumber", "phone": "818-738-0422", "trade": "plumbing",
     "notes": "Plumbing boys",
     "properties": ["2135 Ivar Ave #10 Los Angeles, CA 90068"]},
    {"name": "John Tugman", "phone": "661-373-8633", "trade": "hvac",
     "notes": "Radiant Heat specialist for all units",
     "properties": ["2135 Ivar Ave #10 Los Angeles, CA 90068"]},
    {"name": "Angelo V", "phone": "310-733-8203", "trade": "carpentry",
     "properties": ["218/220 Horizon Ave Venice, CA 90291"]},
    {"name": "Johnny Sterner", "phone": "310-210-6001", "trade": "plumbing",
     "properties": ["218/220 Horizon Ave Venice, CA 90291"]},
    {"name": "Rafael Ibara", "phone": "323-253-5594", "trade": "plumbing",
     "properties": ["218/220 Horizon Ave Venice, CA 90291"]},
    {"name": "Fernando Trinidad", "phone": "805-732-0931", "trade": "electrical",
     "properties": ["218/220 Horizon Ave Venice, CA 90291"]},
    {"name": "South County Exterminators", "phone": "", "trade": "pest_control",
     "properties": ["829 North Bunker Hill Avenue Los Angeles, CA 90012"]},
    {"name": "AC Pros", "phone": "818-342-7767", "trade": "hvac",
     "notes": "Contact Miriam or Amee",
     "properties": ["8342 Kirkwood Dr Los Angeles, CA 90046"]},
    {"name": "Wired Up Electric", "phone": "", "trade": "electrical",
     "notes": "Contact Igor Parchelli",
     "properties": ["8342 Kirkwood Dr Los Angeles, CA 90046"]},
    {"name": "Cali Shades", "phone": "818-836-2404", "trade": "window_treatments",
     "properties": ["8921 Wonderland Park Ave Los Angeles, CA 90046"]},
    {"name": "Gate Safety Centric", "phone": "310-317-7700", "trade": "gate",
     "notes": "Contact Pete Urueta",
     "properties": ["8921 Wonderland Park Ave Los Angeles, CA 90046"]},
    {"name": "Allied Roofing", "phone": "", "trade": "roofing",
     "properties": ["4418 Vantage Ave Studio City, CA 91604"]},
    {"name": "First City Construction", "phone": "", "trade": "general",
     "notes": "Used for multiple WOs historically", "properties": []},
    {"name": "Javi's Painting Inc.", "phone": "", "trade": "painting",
     "properties": []},
    {"name": "SHINE MAINTENANCE", "phone": "", "trade": "general",
     "properties": []},
    {"name": "A1 Service Appliances, Inc.", "phone": "", "trade": "appliance",
     "properties": []},
    {"name": "Yannety Landscape", "phone": "", "trade": "landscaping",
     "properties": ["1616 Ashland Ave Santa Monica, CA 90405"]},
    {"name": "Alfredo Perez", "phone": "310-989-4725", "trade": "pool",
     "properties": ["2123 Linnington Ave Los Angeles, CA 90025"]},
    {"name": "Carlos Aparicio", "phone": "213-909-5456", "trade": "landscaping",
     "properties": ["2123 Linnington Ave Los Angeles, CA 90025"]},
    {"name": "Rightway Apartment Service", "phone": "", "trade": "general",
     "properties": []},
    {"name": "Bresnahan Rain Gutter Cleaning", "phone": "", "trade": "roofing",
     "properties": []},
    {"name": "Ariana Glad Cleaning", "phone": "310-307-9480", "trade": "cleaning",
     "properties": ["2135 Ivar Ave #10 Los Angeles, CA 90068"]},
    {"name": "Jeffery Sanchez", "phone": "818-355-0584", "trade": "landscaping",
     "notes": "Gardener, Federico's son",
     "properties": ["2135 Ivar Ave #10 Los Angeles, CA 90068"]},
    {"name": "Pauls Windows", "phone": "562-400-5199", "trade": "windows",
     "notes": "Worked with Rob de pass",
     "properties": ["2135 Ivar Ave #10 Los Angeles, CA 90068"]},
    {"name": "Luis (LC Construction Inc.)", "phone": "", "trade": "general",
     "notes": "Owner and GC — everything for this property goes through him. "
              "vendor@example.com",
     "properties": ["2312 West Blvd Los Angeles, CA 90016"]},
    {"name": "Jose's Landscaping", "phone": "", "trade": "landscaping",
     "properties": ["742 N Cherokee Los Angeles, CA 90038"]},
    {"name": "Jimmy Moreno", "phone": "", "trade": "pool",
     "notes": "Pool Service",
     "properties": ["742 N Cherokee Los Angeles, CA 90038"]},
    {"name": "John Charles Evans", "phone": "310-487-5209", "trade": "pest_control",
     "notes": "Termite Inspection, renew each year — contact Joe Alvarez",
     "properties": ["8342 Kirkwood Dr Los Angeles, CA 90046"]},
    {"name": "Titan's Plumbing", "phone": "818-300-1868", "trade": "plumbing",
     "notes": "Eduardo Moreno",
     "properties": ["8342 Kirkwood Dr Los Angeles, CA 90046"]},
    {"name": "George Hernandez", "phone": "818-419-9479", "trade": "hvac",
     "properties": ["218/220 Horizon Ave Venice, CA 90291"]},
]

WO_HISTORY = [
    {"vendor": "First City Construction", "property": "2123 Linnington Ave Los Angeles, CA 90025",
     "wo_num": "553-1", "issue": "general", "amount": 641.59, "status": "Completed"},
    {"vendor": "First City Construction", "property": "1827 S Barrington Los Angeles, CA 90025",
     "wo_num": "606-1", "issue": "general", "amount": 37.5, "status": "Completed"},
    {"vendor": "Javi's Painting Inc.", "property": "2135 Ivar Ave #10 Los Angeles, CA 90068",
     "wo_num": "paint-1", "issue": "painting", "amount": 0, "status": "Completed"},
    {"vendor": "A1 Service Appliances, Inc.", "property": "829 North Bunker Hill Avenue Los Angeles, CA 90012",
     "wo_num": "appl-1", "issue": "appliance", "amount": 0, "status": "Completed"},
]


def run():
    conn = sqlite3.connect(DB_PATH)

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS vendors (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL UNIQUE,
            phone        TEXT,
            email        TEXT,
            trade        TEXT,
            notes        TEXT,
            active       INTEGER NOT NULL DEFAULT 1,
            created_at   TEXT
        );
        CREATE TABLE IF NOT EXISTS property_vendors (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            property_address TEXT,
            vendor_id        INTEGER,
            trade            TEXT,
            is_preferred     INTEGER DEFAULT 1,
            source           TEXT,
            UNIQUE(property_address, vendor_id)
        );
        CREATE TABLE IF NOT EXISTS vendor_work_history (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            vendor_id        INTEGER,
            property_address TEXT,
            wo_num           TEXT,
            issue_type       TEXT,
            description      TEXT,
            status           TEXT,
            done_on          TEXT,
            amount           REAL
        );
    """)
    vendor_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(vendors)").fetchall()
    }
    if "active" not in vendor_columns:
        conn.execute(
            "ALTER TABLE vendors ADD COLUMN active "
            "INTEGER NOT NULL DEFAULT 1"
        )
    conn.commit()

    seeded = 0
    errors = 0
    for v in VENDORS:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO vendors (name, phone, trade, notes, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (v["name"], v.get("phone", ""), v["trade"],
                 v.get("notes", ""), datetime.now().isoformat()))
            vendor_id = conn.execute(
                "SELECT id FROM vendors WHERE name = ?", (v["name"],)
            ).fetchone()[0]

            for prop in v.get("properties", []):
                conn.execute(
                    "INSERT OR IGNORE INTO property_vendors "
                    "(property_address, vendor_id, trade, source) "
                    "VALUES (?, ?, ?, ?)",
                    (prop, vendor_id, v["trade"], "property_directory"))
            seeded += 1
        except Exception as e:
            errors += 1
            print(f"Error seeding {v['name']}: {e}")

    # Seed work history
    for h in WO_HISTORY:
        try:
            vid = conn.execute(
                "SELECT id FROM vendors WHERE name = ?", (h["vendor"],)
            ).fetchone()
            if vid:
                existing = conn.execute(
                    "SELECT 1 FROM vendor_work_history "
                    "WHERE wo_num = ? LIMIT 1",
                    (h["wo_num"],),
                ).fetchone()
                if not existing:
                    conn.execute(
                        "INSERT INTO vendor_work_history "
                        "(vendor_id, property_address, wo_num, issue_type, "
                        "status, amount) VALUES (?, ?, ?, ?, ?, ?)",
                        (vid[0], h["property"], h["wo_num"],
                         h["issue"], h["status"], h["amount"]))
        except Exception as e:
            errors += 1
            print(f"Error seeding WO history: {e}")

    if errors:
        conn.rollback()
        conn.close()
        print(
            f"Vendor seed rolled back because {errors} operation(s) failed."
        )
        return False

    conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM vendors").fetchone()[0]
    links = conn.execute("SELECT COUNT(*) FROM property_vendors").fetchone()[0]
    print(f"✅ Vendor database seeded:")
    print(f"   {total} vendors")
    print(f"   {links} property-vendor links")
    conn.close()
    return True


if __name__ == "__main__":
    completed = run()
    if completed:
        print(
            "\nVendor database ready. Rachel will suggest these vendors "
            "on work orders."
        )
    raise SystemExit(0 if completed else 1)
