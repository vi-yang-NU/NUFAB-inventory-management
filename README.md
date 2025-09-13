#
# Written by Vincent Yang
# Sept 9, 2026 
#
# The goal of this is to create an inventory management software for NU FAB.

# The user should be able to scan a barcode that lets us take items out of the inventory or put items back into the inventory. 

# Taking items out of the inventory will charge once the task is marked as completed. 
# There are times that are "on hold" and we will only charge if the user does not return the item. 
# and include awaiting payments 


# File tree 
.
├─ Dockerfile
├─ app.py
├─ nucore_client.py
├─ templates/
│  ├─ base.html
│  ├─ index.html
│  └─ _row.html
├─ config.json         # keep on host, mount into container
└─ data/               # created at runtime
