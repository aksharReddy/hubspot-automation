import requests, os, json

token = os.environ["HUBSPOT_TOKEN"]
headers = {"Authorization": f"Bearer {token}"}

r = requests.get(
    "https://api.hubapi.com/crm/v3/objects/meetings/375850892018",
    headers=headers,
    params={"properties": "hs_meeting_title,hs_meeting_body,hs_meeting_location,hs_meeting_external_url,hs_meeting_outcome,hs_attendee_owner_ids"}
)
print(json.dumps(r.json(), indent=2))
