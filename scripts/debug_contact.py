import requests, os, json

token = os.environ["HUBSPOT_TOKEN"]
headers = {"Authorization": f"Bearer {token}"}

r = requests.get(
    "https://api.hubapi.com/crm/v3/objects/contacts/438670378724",
    headers=headers,
    params={"properties": "phone,mobilephone,hs_whatsapp_phone_number,fax,work_phone,phone_number,hs_phone_number"}
)
print(json.dumps(r.json(), indent=2))
