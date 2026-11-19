import uuid
import requests

SUBSCRIPTION_KEY = '10d9756a74124cc882117853e32a1b7a'
CALLBACK_HOST = 'try-flexibility-february-book.trycloudflare.com'

# Create API User
api_user_id = str(uuid.uuid4())
url = 'https://sandbox.momodeveloper.mtn.com/v1_0/apiuser'
headers = {
    'X-Reference-Id': api_user_id,
    'Ocp-Apim-Subscription-Key': SUBSCRIPTION_KEY,
    'Content-Type': 'application/json'
}
payload = {'providerCallbackHost': CALLBACK_HOST}

r = requests.post(url, json=payload, headers=headers)
print('Create API User:', r.status_code, r.text)

if r.status_code in (200, 201):
    # Create API Key
    key_url = f'https://sandbox.momodeveloper.mtn.com/v1_0/apiuser/{api_user_id}/apikey'
    key_headers = {'Ocp-Apim-Subscription-Key': SUBSCRIPTION_KEY}
    kr = requests.post(key_url, headers=key_headers)
    print('Create API Key:', kr.status_code, kr.text)
    if kr.status_code in (200, 201):
        api_key = kr.json().get('apiKey')
        print()
        print('=== PASTE INTO config/settings.py ===')
        print(f'MOMO_API_USER = "{api_user_id}"')
        print(f'MOMO_API_KEY = "{api_key}"')
        print(f'MOMO_CALLBACK_URL = "https://{CALLBACK_HOST}/api/momo/callback/"')