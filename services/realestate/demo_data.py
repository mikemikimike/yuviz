"""Sample developer for demos and testing: Aaravi Realty, Noida. Every name here is invented.

The projects match the knowledge-base documents the demo agent searches (prices and amenities
live there; this table only carries what the booking tools need)."""

from __future__ import annotations

COMPANY = {
    "company_name": "Aaravi Realty",
    "company_name_hi": "आरवी रियल्टी",
    "timezone": "Asia/Kolkata",
    "sales_phone": "0120 510 8341",
}

# code, name, name_hi, locality, kind, status, summary, meeting_point
PROJECTS = [
    ("GFH", "Greenfield Heights", "ग्रीनफील्ड हाइट्स", "Noida Sector 150", "apartment", "under_construction",
     "2, 3 and 3 plus study BHK apartments in four high-rise towers.", "the model flat at the Greenfield Heights site"),
    ("AVL", "Aaravi Villas", "आरवी विलाज़", "Greater Noida West", "villa", "ready_to_move",
     "3 and 4 bedroom villas in a gated community; phase 1 is ready to move in.", "the Aaravi Villas gate"),
    ("ASQ", "Aaravi Square", "आरवी स्क्वायर", "Noida Sector 62", "commercial", "ready_to_move",
     "Office spaces and retail shops with metro access.", "the Aaravi Square reception"),
]
