import os
import requests
import firebase_admin
from firebase_admin import credentials, db
from flask import Flask, request, jsonify
from datetime import datetime
import pytz
import time
import json
from concurrent.futures import ThreadPoolExecutor
from requests.structures import CaseInsensitiveDict

# Initialize Flask app
app = Flask(__name__)

# Initialize Firebase Admin SDK
cred = credentials.Certificate('serviceAccountKey.json')
firebase_admin.initialize_app(cred, {
    'databaseURL': 'https://myid-networktest-default-rtdb.asia-southeast1.firebasedatabase.app/'
})

# Reference to the root of the database
db_ref = db.reference('/')

# Function to get Myanmar Time
def get_myanmar_time():
    myanmar_tz = pytz.timezone('Asia/Yangon')
    return datetime.now(myanmar_tz)

# Route: /deleteDeviceID
@app.route('/deleteDeviceID', methods=['DELETE'])
def delete_device_id():
    try:
        # Get the username from the query parameters
        username = request.args.get('username')

        if not username:
            return jsonify({'error': 'Username is required'}), 400

        # Reference the users node in Firebase
        users_ref = db.reference('users')
        users_data = users_ref.get()

        if not users_data:
            return jsonify({'error': 'No users found'}), 404

        for user_key, user_info in users_data.items():
            if user_info.get('username') == username:
                # Remove the deviceID field for the user
                users_ref.child(user_key).update({'deviceID': ''})
                return jsonify({'message': f"DeviceID for username '{username}' successfully deleted"}), 200

        return jsonify({'error': f"Username '{username}' not found"}), 404

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Route to add a phone number
@app.route('/addPhoneNumber', methods=['POST'])
def add_phone_number():
    data = request.json
    ph_no = data.get('ph_no')
    sim = data.get('sim')
    access_token = data.get('access_token')
   #date = get_myanmar_time()
    
    if not ph_no or not sim or not access_token:
        return jsonify({"message": "Missing data"}), 400

    # Add or update phone number in the database
    db_ref.child('PhoneNumbers').child(ph_no).set({
        'ph_no': ph_no,
        'sim': sim,
        'access_token': access_token,
        'condition': 'Not Claimed Today',
        'NetworkTest': 'Not Tested'
        
    })
    
    # Perform network test immediately after adding the phone number
    process_network_test(ph_no, sim, access_token)
    
    return jsonify({"message": "Phone number added and network test started"}), 200


# Route to check the condition of a phone number
@app.route('/check', methods=['GET'])
def check_condition():
    ph_no = request.args.get('ph_no')
    if not ph_no:
        # Reference to the /Status node
        status_ref = db.reference('/Status')
        status_data = status_ref.get()

        # Reference to the /apis node
        apis_ref = db.reference('/apis')
        apis_data = apis_ref.get()

        if not status_data:
            return jsonify({"error": "No status data found."}), 404

        if not apis_data:
            return jsonify({"error": "No API data found in /apis."}), 404

        enriched_status = {}
        for key, value in status_data.items():
            # Add API and Name details from /apis if they exist
            api_info = apis_data.get(key, {})
            # Convert all values to string format
            enriched_status[key] = {
                **{k: str(v) for k, v in value.items()},  # Convert existing status details to strings
                "api": api_info.get("api", "N/A"),
                "name": api_info.get("name", "N/A")
            }

        return jsonify({"status": enriched_status}), 200

    # Fetch the condition from Firebase
    phone_data = db.reference('/PhoneNumbers').child(ph_no).get()
    if not phone_data:
        return jsonify({"message": "Phone number not found"}), 404

    condition = phone_data.get('condition', 'No condition found')
    return jsonify({"condition": condition}), 200


@app.route('/startNow', methods=['POST'])
def start_now():
    
    # Using ThreadPoolExecutor to run tasks in parallel
    with ThreadPoolExecutor(max_workers=10) as executor:
        phone_numbers = db_ref.child('PhoneNumbers').get()
        if not phone_numbers:
            return jsonify({"error": "No phone numbers found in the database."}), 404
        
        futures = []
        for ph_no, data in reversed(list(phone_numbers.items())):
            access_token = data['access_token']
            sim = data['sim']
            futures.append(executor.submit(process_phone_number, ph_no, access_token, sim))
        
        for future in futures:
            future.result()

    update_all_finished_time()

    return jsonify({"message": "Automatic tasks started for all phone numbers."}), 200

# Function to process both daily quest and network test for a single phone number
def process_phone_number(ph_no, access_token, sim):
    # Get current date in Myanmar time
    current_date = get_myanmar_time().strftime('%Y-%m-%d')

    # Retrieve the finish_time from Firebase
    phone_data = db_ref.child('PhoneNumbers').child(ph_no).get()
    finish_time = phone_data.get('finish_time', '')

    if finish_time:
        # Extract the date part from finish_time for comparison
        finish_date = finish_time.split(' ')[0]
        
        # Skip the job if the current date matches the finish_time date
        if finish_date == current_date:
            print(f"Skipping job for {ph_no} as it is already completed today.")
            return
    
    # Continue processing if finish_time does not match current date
    process_daily_quest(ph_no, access_token)
    process_network_test(ph_no, sim, access_token)
    
    # Log the finish time after both tasks are completed
    finish_time = get_myanmar_time().strftime('%Y-%m-%d %H:%M:%S')
    update_finish_time_in_firebase(ph_no, finish_time)

# Function to process the daily quest
def process_daily_quest(ph_no, access_token):
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }
    
    # GET request to find the current day
    get_info_url = f'https://apis.mytel.com.mm/myid/daily-quest/v3.1/api/quest/get-info-daily-quest/{ph_no}?language=EN'
    response = retry_request('GET', get_info_url, headers=headers)
    
    if not response:
        update_condition_in_firebase(ph_no, 'Error occurred during daily quest request', "Quest")
        print(f"Error occurred during daily quest request for {ph_no}")
        return

    data = response.json()
    days_list = data.get('result', {}).get('daysList', [])
    current_day = None
    for day_data in days_list:
        if day_data.get('currentDay', False):
            current_day = day_data.get('day')
            break

    if not current_day:
        error_message = data.get('message', 'No current day found')
        update_condition_in_firebase(ph_no, error_message, "Quest")
        print(f"Daily quest error for {ph_no}: {error_message}")
        return

    # POST request to claim reward
    claim_url = 'https://apis.mytel.com.mm/myid/daily-quest/v3.1/api/quest/send-reward'
    payload = {
        'msisdn': ph_no,
        'language': 'EN',
        'dayNumber': current_day
    }
    post_response = retry_request('POST', claim_url, headers=headers, json_data=payload)
    
    if not post_response:
        update_condition_in_firebase(ph_no, 'Error occurred during reward claim', "Quest")
        print(f"Error occurred during reward claim for {ph_no}")
        return

    post_data = post_response.json()
    message = post_data.get('message', '')

    if 'HL2' in message:
        condition = 'HL2 Package is Required'
    elif 'Already' in message:
        condition = 'Already Claimed'
    elif post_data.get('result') == True:
        condition = 'Claimed for Today'
    else:
        condition = f"Error: {message}"
    
    update_condition_in_firebase(ph_no, condition, "Quest")
    print(f"Daily quest update for {ph_no}: {condition}")

# Function to process the network test
def process_network_test(ph_no, sim, access_token):
    lphone = ph_no.lstrip('0')  # Strip leading zero from the phone number
    url = "https://apis.mytel.com.mm/network-test/v3/submit"
    headers = CaseInsensitiveDict()
    headers["Host"] = "apis.mytel.com.mm"
    headers["accept"] = "application/json"
    headers["accept-language"] = "EN"
    headers["authorization"] = f"Bearer {access_token}"
    headers["Content-Type"] = "application/json"
    headers["accept-encoding"] = "gzip"
    headers["user-agent"] = "okhttp/4.9.1"

    data = json.dumps({
        "cellId": "30824726",
        "deviceModel": "IPHONE 14",
        "downloadSpeed": 99.5,
        "enb": "120409",
        "latency": 62.625,
        "latitude": "15.3943318",
        "location": "Mon State, Myanmar (Burma)",
        "longitude": "97.8913799",
        "msisdn": f"+95{lphone}",
        "networkType": "_4G",
        "operator": sim,
        "requestId": "12be4567-e89b-12d3-a456-426655444212",
        "requestTime": "2023-10-19T05:51:08.433",
        "rsrp": "-97",
        "testRecordID": "12be4567-e89b-12d3-a456-426655444212",
        "township": "Mon State",
        "uploadSpeed": 95.1
    })

    response = retry_request('POST', url, headers=headers, data=data)
    if response and response.status_code == 200:
        update_condition_in_firebase(ph_no, 'Network test success', "NetworkTest")
        print(f"Network test successful for {ph_no}")
    elif response:
    # If response is not None but indicates failure
        try:
            error_message = response.json().get('message', 'Network test failed')
        except Exception:
            error_message = 'Network test failed and response parsing error'
        update_condition_in_firebase(ph_no, error_message, "NetworkTest")
        print(f"Network test failed for {ph_no}: {error_message}")
    else:
    # Handle NoneType response
        update_condition_in_firebase(ph_no, 'No response from server', "NetworkTest")
        print(f"Network test failed for {ph_no}: No response from server")
        
# Function to retry HTTP requests on failure (e.g., status code 502)
def retry_request(method, url, headers=None, json_data=None, data=None, retries=3, delay=2):
    for attempt in range(retries):
        try:
            if method == 'GET':
                response = requests.get(url, headers=headers)
            elif method == 'POST':
                if json_data:
                    response = requests.post(url, headers=headers, json=json_data)
                else:
                    response = requests.post(url, headers=headers, data=data)
            
            if response.status_code == 200:
                return response
            elif response.status_code == 502:
                print(f"Received 502 for {url}, retrying after {delay} seconds...")
                time.sleep(delay)
            else:
                print(f"Error {response.status_code} for {url}: {response.text}")
                return response
        except Exception as e:
            print(f"Exception occurred: {str(e)}")
            return None

# Function to update condition in Firebase
def update_condition_in_firebase(ph_no, condition, what):
    if what == "NetworkTest":
        db_ref.child('PhoneNumbers').child(ph_no).child('NetworkTest').set(condition)
    elif what == "Quest":
        db_ref.child('PhoneNumbers').child(ph_no).child('Quest').set(condition)

# Function to update finish time in Firebase
def update_finish_time_in_firebase(ph_no, finish_time):
    db_ref.child('PhoneNumbers').child(ph_no).child('finish_time').set(finish_time)

# Function to update overall statistics in the 'All Finished Time' node
def update_all_finished_time():
    phone_numbers = db_ref.child('PhoneNumbers').get()
    total_count = len(phone_numbers)
    completed_count = sum(1 for data in phone_numbers.values() if 'finish_time' in data)
    success_count = sum(1 for data in phone_numbers.values() if data.get('Quest') == 'Claimed for Today')
    fail_count = total_count - success_count
    fail_numbers = [ph_no for ph_no, data in phone_numbers.items() if data.get('Quest') != 'Claimed for Today']
    
    start_time = get_myanmar_time()
    end_time = get_myanmar_time()
    
    all_finished_time_data = {
        'total_phone_numbers': total_count,
        'completed_number_count': completed_count,
        'success_number_count': success_count,
        'fail_number_count': fail_count,
        'fail_numbers': fail_numbers,
        'duration': f"{start_time.strftime('%Y-%m-%d %H:%M:%S')} to {end_time.strftime('%Y-%m-%d %H:%M:%S')}",
        'date': end_time.strftime('%Y-%m-%d')
    }
    
    db_ref.child('AllFinishedTime').set(all_finished_time_data)

# Route to handle login functionality
@app.route('/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username')
    pw = data.get('pw')
    deviceID = data.get('deviceID')

    if not username or not pw or not deviceID:
        return jsonify({"message": "Missing username, password, or deviceID"}), 400

    users_ref = db.reference('users')
    users_data = users_ref.get()

    if not users_data:
        return jsonify({"message": "No users found."}), 404

    for user_key, user_info in users_data.items():
        if user_info.get('username') == username and user_info.get('pw') == pw:
            stored_deviceID = user_info.get('deviceID', '')

            # Check if deviceID exists and whether it matches
            if stored_deviceID and stored_deviceID != deviceID:
                return jsonify({"message": "This account was logged in on another device"}), 403

            # If no deviceID or it matches, update the deviceID in the database
            if not stored_deviceID or stored_deviceID == '':
                users_ref.child(user_key).update({'deviceID': deviceID})

            return jsonify({"message": "Login Successful"}), 200

    return jsonify({"message": "Wrong username or password"}), 401

# Route to create a new account
@app.route('/createAcc', methods=['POST'])
def create_account():
    data = request.json
    username = data.get('username')
    pw = data.get('pw')

    if not username or not pw:
        return jsonify({"message": "Missing username or password"}), 400

    users_ref = db.reference('users')

    # Check if the username already exists
    users_data = users_ref.get()
    for user_key, user_info in users_data.items():
        if user_info.get('username') == username:
            return jsonify({"message": "Username already exists"}), 409

    # Add the new user to the database
    new_user_ref = users_ref.push()
    new_user_ref.set({
        'username': username,
        'pw': pw,
        'deviceID': ''
    })

    return jsonify({"message": "Account created successfully"}), 201

# Route to change the password for an existing account
@app.route('/changePassword', methods=['POST'])
def change_password():
    data = request.json
    username = data.get('username')
    pw = data.get('pw')

    if not username or not pw:
        return jsonify({"message": "Missing username or password"}), 400

    users_ref = db.reference('users')
    users_data = users_ref.get()

    # Check if the username exists
    for user_key, user_info in users_data.items():
        if user_info.get('username') == username:
            # Update the password for the existing username
            users_ref.child(user_key).update({'pw': pw})
            return jsonify({"message": "Password updated successfully"}), 200

    return jsonify({"message": "Username does not exist"}), 404

# Route to get all user accounts excluding "ADMIN"
@app.route('/getAccounts', methods=['GET'])
def get_accounts():
    users_ref = db.reference('users')
    users_data = users_ref.get()

    if not users_data:
        return jsonify({"message": "No users found"}), 404

    account_list = []

    # Loop through users and exclude "ADMIN"
    for user_key, user_info in users_data.items():
        if user_info.get('username') != "ADMIN":
            account_list.append({
                'username': user_info.get('username'),
                'pw': user_info.get('pw'),
                'deviceID': user_info.get('deviceID', '')  # Include deviceID if it exists, otherwise None
            })

    return jsonify({"AccList": account_list}), 200
@app.route('/delAcc', methods=['DELETE'])
def delete_account():
    # Get the username and password from the query parameters
    username = request.args.get('username')
    pw = request.args.get('pw')
    
    if not username or not pw:
        return jsonify({"message": "Missing username or password"}), 400

    # Reference to the users node in Firebase
    users_ref = db_ref.child('users')

    # Retrieve all users from the database
    users_data = users_ref.get()

    if not users_data:
        return jsonify({"message": "No users found"}), 404

    # Iterate through the users to find the matching username and password
    user_found = False
    for user_key, user_info in users_data.items():
        if user_info.get('username') == username and user_info.get('pw') == pw:
            # Delete the user from Firebase if the username and password match
            users_ref.child(user_key).delete()
            user_found = True
            break

    if user_found:
        return jsonify({"message": f"User '{username}' deleted successfully"}), 200
    else:
        return jsonify({"message": "Username or password does not match"}), 404

@app.route('/checkStatusForToday', methods=['GET'])
def check_status_for_today():
    current_date = get_myanmar_time().strftime('%Y-%m-%d')
    
    # Get the current date in the format YYYY-MM-DD
    #today = datetime.now().strftime('%Y-%m-%d')
    
    # Reference the PhoneNumbers node in Firebase
    phone_numbers_ref = db_ref.child('PhoneNumbers')
    phone_numbers_data = phone_numbers_ref.get()

    if not phone_numbers_data:
        return jsonify({"message": "No phone numbers found"}), 404

    # Initialize counters
    total_numbers = 0
    completed_numbers = 0
    success_count = 0
    fail_count = 0
    fail_numbers_list = []

    # Iterate through all phone numbers in the database
    for ph_no, ph_data in phone_numbers_data.items():
        total_numbers += 1
        finish_time = ph_data.get('finish_time', '')
        network_test = ph_data.get('NetworkTest', '')

        if finish_time:
            finish_date = finish_time.split(' ')[0]  # Extract date from finish_time
            if finish_date == current_date:
                completed_numbers += 1

        # Check the NetworkTest result for success or failure
        if network_test == 'Network test success':
            success_count += 1
        else:
            fail_count += 1
            fail_numbers_list.append(ph_no)

    # Create response with the status summary
    response = {
        "date" : finish_date,
        "total_numbers": total_numbers,
        "completed_numbers": completed_numbers,
        "success_count": success_count,
        "fail_count": fail_count,
        "fail_numbers": fail_numbers_list
    }

    return jsonify(response), 200


@app.route('/getAPI', methods=['GET'])
def get_api():
    try:
        # Fetch the '/api' node from Firebase
        api_data = db.reference('/api').get()
        
        # Check if the key "api" exists in the fetched data
        if api_data and "api" in api_data:
            return jsonify({"api": api_data["api"]}), 200
        else:
            return jsonify({"error": "Key 'api' not found in the database."}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500
# Route: /hi
@app.route('/hi', methods=['GET'])
def say_hi():
    return "hi back", 200
# Run the Flask app
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
