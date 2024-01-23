#!/usr/bin/env python3
"""
Copyright (c) 2023 Cisco and/or its affiliates.
This software is licensed to you under the terms of the Cisco Sample
Code License, Version 1.1 (the "License"). You may obtain a copy of the
License at
https://developer.cisco.com/docs/licenses
All use of the material herein must be in accordance with the terms of
the License. All rights not expressly granted by the License are
reserved. Unless required by applicable law or agreed to separately in
writing, software distributed under the License is distributed on an "AS
IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
or implied.
"""

__author__ = "Trevor Maco <tmaco@cisco.com>"
__copyright__ = "Copyright (c) 2023 Cisco and/or its affiliates."
__license__ = "Cisco Sample Code License, Version 1.1"

import json
import os
from dotenv import load_dotenv

from flask import Flask, request, redirect, session, render_template
from requests_oauthlib import OAuth2Session

# initialize variables for URLs and Webex App
PUBLIC_URL = 'http://0.0.0.0:5500'
REDIRECT_URI = PUBLIC_URL + '/callback'

AUTHORIZATION_BASE_URL = 'https://api.ciscospark.com/v1/authorize'
TOKEN_URL = 'https://api.ciscospark.com/v1/access_token'
SCOPE = ['meeting:admin_recordings_read', 'meeting:admin_preferences_read']

os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# Create the web application instance
app = Flask(__name__)
app.secret_key = '123456789012345678901234'

# Load ENV Variable
load_dotenv()
WEBEX_CLIENT_ID = os.getenv("WEBEX_CLIENT_ID")
WEBEX_CLIENT_SECRET = os.getenv("WEBEX_CLIENT_SECRET")

script_dir = os.path.dirname(os.path.abspath(__file__))
tokens_file = os.path.join(script_dir, 'tokens.json')

@app.route("/")
def index():
    """
    Step 1: User Authorization.
    Redirect the user/resource owner to the OAuth provider (i.e. Webex Teams)
    using a URL with a few key OAuth parameters.
    :return: redirect to authorization url
    """
    teams = OAuth2Session(WEBEX_CLIENT_ID, scope=SCOPE, redirect_uri=REDIRECT_URI)
    authorization_url, state = teams.authorization_url(AUTHORIZATION_BASE_URL)
    session['oauth_state'] = state
    print("Storing state: ", state, "\nroot route is re-directing to ", authorization_url,
          " and had sent redirect uri: ", REDIRECT_URI)
    return redirect(authorization_url)


# Step 2: User authorization, this happens on the provider.

@app.route("/callback", methods=["GET"])
def callback():
    """
    Step 3: Retrieving an access token.
    The user has been redirected back from the provider to your registered
    callback URL. With this redirection comes an authorization code included
    in the redirect URL. We will use that to obtain an access token.
    :return: redirect to main app, now with a token
    """
    auth_code = OAuth2Session(WEBEX_CLIENT_ID, state=session['oauth_state'], redirect_uri=REDIRECT_URI)
    tokens = auth_code.fetch_token(token_url=TOKEN_URL, client_secret=WEBEX_CLIENT_SECRET,
                                   authorization_response=request.url)

    with open(tokens_file, 'w') as json_file:
        json.dump(tokens, json_file)

    return render_template('success.html')


if __name__ == "__main__":
    # Spinning up Flask server for admin to perform OAuth
    print("Using PUBLIC_URL: ", PUBLIC_URL)
    print("Using redirect URI: ", REDIRECT_URI)
    app.run(host='0.0.0.0', port=5500, debug=True)
