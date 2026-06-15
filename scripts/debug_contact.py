import requests, os, json

token = os.environ["HUBSPOT_TOKEN"]
headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

# Search for contact by phone number
r = requests.post(
    "https://api.hubapi.com/crm/v3/objects/contacts/search",
    headers=headers,
    json={
        "filterGroups": [{"filters": [{"propertyName": "phone", "operator": "EQ", "value": "+919014049975"}]}],
        "properties": ["firstname", "lastname", "phone", "mobilephone", "email"]
    }
)
print("By phone:", json.dumps(r.json(), indent=2))

r2 = requests.post(
    "https://api.hubapi.com/crm/v3/objects/contacts/search",
    headers=headers,
    json={
        "filterGroups": [{"filters": [{"propertyName": "mobilephone", "operator": "EQ", "value": "+919014049975"}]}],
        "properties": ["firstname", "lastname", "phone", "mobilephone", "email"]
    }
)
print("By mobile:", json.dumps(r2.json(), indent=2))

# Also check all contacts on the meeting
r3 = requests.post(
    "https://api.hubapi.com/crm/v3/associations/meetings/contacts/batch/read",
    headers=headers,
    json={"inputs": [{"id": "375850892018"}]}
)
print("Meeting contacts:", json.dumps(r3.json(), indent=2))
