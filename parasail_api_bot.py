import asyncio
import json
import os
import time
import requests
from web3 import Web3
from eth_account import Account, messages
from dotenv import load_dotenv
import random # For jitter in backoff

# --- Load Environment Variables ---
load_dotenv()

# --- Configuration Management ---
CONFIG_FILE = 'config.json'
BASE_URL = 'https://www.parasail.network/api'

class ParasailNodeBot:
    def __init__(self):
        # Load dynamic config (wallet_address, bearer_token) from config.json
        self.config = self._load_config()

        # Load private key from .env file
        self.private_key = os.getenv("PRIVATE_KEY")
        if not self.private_key:
            self.log("Error: PRIVATE_KEY not found in .env file.")
            self.log("Please ensure your .env file is configured correctly.")
            exit(1)

        self.w3 = Web3() # Web3 instance for signing

        # Initialize wallet with private key from .env
        try:
            self.wallet = Account.from_key(self.private_key)
            # Ensure wallet_address in config.json is consistent with the private key
            if self.config.get("wallet_address") != self.wallet.address:
                self.config["wallet_address"] = self.wallet.address
                self._save_config(self.config) # Save updated config
        except Exception as e:
            self.log(f"Error initializing wallet from PRIVATE_KEY: {e}")
            self.log("Please ensure PRIVATE_KEY in .env is correct and has no '0x' prefix.")
            exit(1)

        self.log(f"Wallet Address: {self.config.get('wallet_address')}")
        self.countdown_remaining_seconds = 0
        self.countdown_task = None
        self.stats_task = None

        # --- Retry Configuration ---
        self.max_retries = 5
        self.initial_delay = 5 # seconds

    def _load_config(self):
        try:
            if not os.path.exists(CONFIG_FILE):
                # Create an empty config if it doesn't exist
                initial_config = {
                    "wallet_address": "",
                    "bearer_token": ""
                }
                with open(CONFIG_FILE, 'w') as f:
                    json.dump(initial_config, f, indent=2)
                self.log(f"Created empty {CONFIG_FILE}. It will be populated automatically.")
                return initial_config

            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            self.log(f"Error loading config from {CONFIG_FILE}: {e}")
            self.log("Please check if config.json is a valid JSON file.")
            exit(1)

    def _save_config(self, config):
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(config, f, indent=2)
        except Exception as e:
            self.log(f"Error saving config to {CONFIG_FILE}: {e}")

    def log(self, message):
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        print(f"[{timestamp}] {message}")

    async def _send_api_request(self, method, endpoint, data=None, headers=None, retry_count=0):
        url = f"{BASE_URL}{endpoint}"
        _headers = {"Content-Type": "application/json"}
        if self.config.get("bearer_token"):
            _headers["Authorization"] = f"Bearer {self.config['bearer_token']}"
        if headers:
            _headers.update(headers)

        try:
            if method == 'GET':
                response = requests.get(url, headers=_headers, timeout=10) # Added timeout
            elif method == 'POST':
                response = requests.post(url, headers=_headers, json=data, timeout=10) # Added timeout
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
            return response.json()
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:
                if retry_count < self.max_retries:
                    current_delay = self.initial_delay * (2 ** retry_count)
                    jitter = random.uniform(0.5, 1.5) # Add random jitter
                    wait_time = current_delay * jitter
                    self.log(f"API request to {endpoint} failed with 429 (Too Many Requests).")
                    self.log(f"Response body: {e.response.text.strip()[:200]}...") # Log part of response
                    self.log(f"Retrying in {wait_time:.2f} seconds (attempt {retry_count + 1}/{self.max_retries})...")
                    await asyncio.sleep(wait_time)
                    return await self._send_api_request(method, endpoint, data, headers, retry_count + 1)
                else:
                    self.log(f"API request to {endpoint} failed with 429 after {self.max_retries} retries.")
                    raise # Re-raise if max retries exhausted
            elif e.response.status_code == 401:
                # Handle token expiry specifically at the API call level if not already handled higher up
                self.log(f"API request to {endpoint} failed with 401 (Unauthorized). Token might be expired.")
                # This needs to be handled by the specific method that called this, to re-verify.
                raise e # Re-raise 401 to be caught by specific method (e.g., check_in)
            else:
                self.log(f"API request to {endpoint} failed: {e}")
                self.log(f"Response status: {e.response.status_code}")
                self.log(f"Response body: {e.response.text}")
                raise
        except requests.exceptions.RequestException as e:
            self.log(f"API request to {endpoint} failed: {e}")
            raise

    async def generate_signature(self):
        message_to_sign = (
            "By signing this message, you confirm that you agree to the Parasail Terms of Service.\n\n"
            "Parasail (including the Website and Parasail Smart Contracts) is not intended for:\n"
            "(a) access and/or use by Excluded Persons;\n"
            "(b) access and/or use by any person or entity in, or accessing or using the Website from, an Excluded Jurisdiction.\n\n"
            "Excluded Persons are prohibited from accessing and/or using Parasail (including the Website and Parasail Smart Contracts).\n\n"
            "For full terms, refer to: https://parasail.network/Parasail_User_Terms.pdf"
        )
        # Using eth_account.messages.encode_defunct for personal_sign
        encoded_message = messages.encode_defunct(text=message_to_sign)
        signed_message = self.w3.eth.account.sign_message(encoded_message, private_key=self.private_key)
        
        # The JS bot returns an object with address, msg, signature
        # We need to ensure the signature is hex format without '0x' if that's what API expects
        # Based on previous output, just signature.hex() should be fine, API will handle 0x
        return {
            "address": self.wallet.address,
            "msg": message_to_sign,
            "signature": signed_message.signature.hex()
        }

    async def verify_user(self):
        self.log("Verifying user and obtaining bearer token...")
        try:
            signature_data = await self.generate_signature()
            
            # The JS bot sends "signatureData" object directly to /auth/verify endpoint
            # which aligns with `data` parameter in _send_api_request
            response = await self._send_api_request('POST', '/user/verify', signature_data)
            
            self.config["bearer_token"] = response.get("token") # Note: JS uses 'token', not 'bearer_token' in response
            # self.config["wallet_address"] is already set and consistent by now
            self._save_config(self.config)
            self.log("User verified. Bearer token obtained and saved.")
            return True
        except Exception as e:
            self.log(f"User verification failed: {e}")
            return False

    async def onboard_node(self):
        self.log("Attempting to onboard node...")
        try:
            response = await self._send_api_request('POST', '/v1/node/onboard', {"address": self.config["wallet_address"]})
            self.log(f"Node onboarded successfully: {response.get('message', 'No message')}")
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                self.log("Token expired during onboarding. Attempting to refresh token...")
                success = await self.verify_user()
                if success:
                    self.log("Token refreshed. Retrying onboarding...")
                    return await self.onboard_node() # Retry the current operation
                else:
                    self.log("Failed to refresh token. Cannot onboard node.")
                    raise
            else:
                self.log(f"Node onboarding failed: {e}")
                raise
        except Exception as e:
            self.log(f"Node onboarding failed: {e}")
            raise

    async def check_in(self):
        self.log("Performing node check-in (power clicker equivalent)...")
        try:
            response = await self._send_api_request('POST', '/v1/node/check_in', {"address": self.config["wallet_address"]})
            self.log(f"Node checked in successfully. Points: {response.get('points', 'N/A')}, Message: {response.get('message', 'No message')}")
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                self.log("Token expired during check-in. Attempting to refresh token...")
                success = await self.verify_user()
                if success:
                    self.log("Token refreshed. Retrying check-in...")
                    return await self.check_in() # Retry the current operation
                else:
                    self.log("Failed to refresh token. Cannot check in node.")
                    raise
            else:
                self.log(f"Node check-in failed: {e}")
                raise
        except Exception as e:
            self.log(f"Node check-in failed: {e}")
            raise

    async def get_node_stats(self):
        self.log("Fetching node stats...")
        try:
            # JS uses /v1/node/node_stats with address as param, and Authorization header
            stats = await self._send_api_request('GET', '/v1/node/node_stats', headers={"Accept": "application/json, text/plain, */*"}, data={"address": self.config["wallet_address"]}) # data= is for POST, for GET it's params
            
            # For GET requests with query parameters, requests.get takes 'params' not 'data'.
            # Adjusting _send_api_request or explicitly call requests.get here with params.
            # Let's adjust _send_api_request to handle 'params' for GET correctly.
            # For now, will explicitly pass params in the call.
            url = f"{BASE_URL}/v1/node/node_stats"
            _headers = {"Content-Type": "application/json"}
            if self.config.get("bearer_token"):
                _headers["Authorization"] = f"Bearer {self.config['bearer_token']}"
            
            # Directly call requests.get with params for node stats
            response = requests.get(url, params={"address": self.config["wallet_address"]}, headers=_headers, timeout=10)
            response.raise_for_status()
            stats = response.json()


            # The JS bot updates a UI, here we'll just log the key stats
            self.log(f"Node Stats - Has Node: {stats.get('has_node', 'N/A')}, Address: {stats.get('node_address', 'N/A')}, Points: {stats.get('points', 'N/A')}")
            self.log(f"Pending Rewards: {stats.get('pending_rewards', 'N/A')}, Total Distributed: {stats.get('total_distributed', 'N/A')}")
            self.log(f"Last Check-in: {time.ctime(stats['last_checkin_time']) if 'last_checkin_time' in stats and stats['last_checkin_time'] else 'N/A'}")
            self.log(f"Card Count: {stats.get('card_count', 'N/A')}")
            
            return stats
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                self.log("Token expired during stats fetch. Attempting to refresh token...")
                success = await self.verify_user()
                if success:
                    self.log("Token refreshed. Retrying stats fetch...")
                    return await self.get_node_stats() # Retry the current operation
                else:
                    self.log("Failed to refresh token. Cannot fetch node stats.")
                    raise
            else:
                self.log(f"Failed to fetch node stats: {e}")
                self.log(f"Response status: {e.response.status_code}")
                self.log(f"Response body: {e.response.text}")
                raise
        except Exception as e:
            self.log(f"Failed to fetch node stats: {e}")
            return {}

    async def start_countdown(self):
        if self.countdown_task:
            self.countdown_task.cancel()
        if self.stats_task:
            self.stats_task.cancel()

        initial_stats = await self.get_node_stats()
        # The JS logic calculates remaining seconds using next_checkin_time and current time
        # The API's /node/stats endpoint returns `next_checkin_timestamp` in milliseconds.
        if initial_stats and 'next_checkin_timestamp' in initial_stats and initial_stats['next_checkin_timestamp'] is not None:
            next_checkin_ms = initial_stats['next_checkin_timestamp']
            current_ms = time.time() * 1000
            self.countdown_remaining_seconds = max(0, int((next_checkin_ms - current_ms) / 1000))
            self.log(f"Next routine tasks in {self.countdown_remaining_seconds} seconds (approx. {self.countdown_remaining_seconds / 3600:.2f} hours).")
        else:
            self.log("Could not determine next check-in time from stats. Scheduling in 12 hours (fallback).")
            self.countdown_remaining_seconds = 12 * 3600 # Fallback to 12 hours

        self.countdown_task = asyncio.create_task(self._countdown_loop())
        self.stats_task = asyncio.create_task(self._stats_update_loop())


    async def _countdown_loop(self):
        while self.countdown_remaining_seconds > 0:
            await asyncio.sleep(min(self.countdown_remaining_seconds, 60)) # Sleep up to 1 minute
            self.countdown_remaining_seconds -= min(self.countdown_remaining_seconds, 60)
        
        self.log('Time to perform routine tasks!')
        await self.perform_routine_tasks()


    async def _stats_update_loop(self):
        while True:
            await asyncio.sleep(60) # Update every 60 seconds
            try:
                await self.get_node_stats()
            except Exception as e:
                self.log(f"Stats update loop failed: {e}")

    async def perform_routine_tasks(self):
        self.log("Performing routine tasks (onboard, check-in)...")
        try:
            await self.onboard_node()
            await asyncio.sleep(2) # Short delay between operations
            await self.check_in()
            await asyncio.sleep(2)
            await self.start_countdown() # Schedule next routine
        except Exception as e:
            self.log(f"Routine tasks failed: {e}")

    async def start(self):
        self.log("Starting Parasail Node Bot (Python equivalent)")
        try:
            # Check for bearer token and verify if missing or expired
            if not self.config.get("bearer_token"):
                self.log("No bearer token found. Attempting to verify user...")
                success = await self.verify_user()
                if not success:
                    self.log("Failed to obtain bearer token. Please check private key and network connection.")
                    return # Exit if we can't get token
            else:
                # Attempt to get stats to see if existing token is valid
                try:
                    await self.get_node_stats()
                    self.log("Existing bearer token is valid.")
                except requests.exceptions.HTTPError as e:
                    if e.response.status_code == 401:
                        self.log("Existing bearer token expired. Attempting to re-verify user...")
                        success = await self.verify_user()
                        if not success:
                            self.log("Failed to re-verify user and obtain new bearer token.")
                            return # Exit if we can't get token
                    else:
                        self.log(f"Error checking existing token (status {e.response.status_code}): {e.response.text}")
                        # If other error, still try to proceed, maybe it's temporary
                except Exception as e:
                    self.log(f"Error checking existing token: {e}")
                    # If other error, still try to proceed

            self.log(f"Wallet Address: {self.config.get('wallet_address')}")

            # Initial tasks after ensuring token
            await self.onboard_node()
            await asyncio.sleep(2)
            await self.check_in()
            await asyncio.sleep(2)

            await self.start_countdown() # Start the main operational loop

            # Keep the bot running indefinitely
            while True:
                await asyncio.sleep(3600) # Sleep for a long time, tasks are scheduled independently

        except Exception as e:
            self.log(f"Initialization failed: {e}")

async def main():
    bot = ParasailNodeBot()
    await bot.start()

if __name__ == "__main__":
    asyncio.run(main())
