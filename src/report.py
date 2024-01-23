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
import math
import os
import sys
import threading
import time
from datetime import datetime, timedelta

import pandas as pd
import pytz
import requests
from dotenv import load_dotenv
from requests_oauthlib import OAuth2Session
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress
from rich.prompt import IntPrompt

import config

### Load env variables
load_dotenv()
WEBEX_CLIENT_ID = os.getenv("WEBEX_CLIENT_ID")
WEBEX_CLIENT_SECRET = os.getenv("WEBEX_CLIENT_SECRET")

### Global Variables
# Webex URLS
TOKEN_URL = 'https://api.ciscospark.com/v1/access_token'
BASE_URL = 'https://webexapis.com/v1/'

# Webex API Restriction of 30 days worth of data
MAX_DAYS = 30

### One time actions
script_dir = os.path.dirname(os.path.abspath(__file__))
report_folder = os.path.join(script_dir, 'reports')
tokens_file = os.path.join(script_dir, 'tokens.json')

# Rich console instance
console = Console()


def calculate_iso_timestamps(current_time, days_ago):
    """
    Calculate To and From ISO 8601 timestamps for the last "days_ago" from the current timestamps
    :param current_time: Current time stamp to calculate 'days_ago' from
    :param days_ago: How many days in the past to calculate the 'from' timestamp
    :return: To and From timestamps in ISO 8601 format
    """
    # Calculate the timestamp for 'days_ago' days ago
    from_time = current_time - timedelta(days=days_ago)

    # Truncate seconds and fractions of a second
    current_time = current_time.replace(microsecond=0)
    from_time = from_time.replace(microsecond=0)

    # Format the timestamps as ISO 8601 strings
    current_time_iso = current_time.isoformat()
    from_time_iso = from_time.isoformat()

    return from_time_iso, current_time_iso


def refresh_token(tokens):
    """
    Refresh Webex token if primary token is expired (assumes refresh token is valid)
    :param tokens: Primary and Refresh Tokens
    :return: New set of tokens
    """
    refresh_token = tokens['refresh_token']
    extra = {
        'client_id': WEBEX_CLIENT_ID,
        'client_secret': WEBEX_CLIENT_SECRET,
        'refresh_token': refresh_token,
    }
    auth_code = OAuth2Session(WEBEX_CLIENT_ID, token=tokens)
    new_teams_token = auth_code.refresh_token(TOKEN_URL, **extra)

    # store away the new token
    with open(tokens_file, 'w') as json_file:
        json.dump(new_teams_token, json_file)

    console.print("- [green]A new token has been generated and stored in `tokens.json`[/]")
    return new_teams_token


def get_wrapper(url, token, params):
    """
    REST Get API Wrapper, includes support for paging, 429 rate limiting, and error handling
    :param url: Resource URL
    :param token: Webex OAuth Token
    :param params: REST API Query Params
    :return: Response Payload (aggregated if multiple pages present)
    """
    # Build Get Request Components
    results = {}
    next_url = f'{BASE_URL}{url}'
    headers = {'Authorization': f'Bearer {token}'}
    retry_count = 0

    while next_url:
        response = requests.get(url=next_url, headers=headers, params=params)

        if response.status_code == 200:
            # 200 response, A okay!
            response_data = response.json()

            # Combine like fields across multiple pages, create an aggregated structure
            for val in response_data:
                if val in results:
                    results[val].extend(response_data[val])
                else:
                    results[val] = response_data[val]

            # Check if there is a next page
            next_url = get_next_page_url(response.headers.get('link'))

            # Clear params to avoid 4XX errors
            if next_url:
                params = {}
        elif response.status_code == 429 and retry_count < 10:
            # Handle 429 Too Many Requests error (10 maximum retries to avoid infinite loops)
            console.print("\n[orange]Rate limit exceeded. Waiting for retry...[/]")
            retry_count += 1
            sleep_time = int(
                response.headers.get('Retry-After', 5))  # Default to 5 seconds if Retry-After is not provided
            time.sleep(sleep_time)
        else:
            # Print failure message on error
            console.print("\n[red]Request FAILED: [/]" + str(response.status_code))
            console.print(response.text)
            console.print(f"\nAPI Response Headers: {response.headers}")
            return None

    return results


def get_next_page_url(link_header):
    """
    Get the next page URL (if pagination present in response within get_wrapper), return next page URL
    :param link_header: Original response headers (contains 'link' element with next page URL)
    :return: Next page URL
    """
    if link_header:
        # Extract get link within links header
        links = link_header.split(',')
        for link in links:
            link_info = link.split(';')
            # Return raw link, ignore special characters and other surrounding characters
            if len(link_info) == 2 and 'rel="next"' in link_info[1]:
                return link_info[0].strip('<> ')
    return None


def get_site_list(token):
    """
    Get list of sites user has access too (https://developer.webex.com/docs/api/v1/meeting-preferences/get-site-list)
    :param token: Webex OAuth Token
    :return: List of Webex Sites
    """
    with console.status("Getting Webex Sites..."):
        response = get_wrapper('meetingPreferences/sites', token, {})

    if response:
        return response['sites']
    else:
        return None


def get_audit_report(token, recording_id, recording_info, progress, task):
    """
    Get recording access history (audit) - multithreading, extract last time recording was accessed (https://developer.webex.com/docs/api/v1/recording-report/get-recording-audit-report-details)
    :param token: Webex OAuth token
    :param recording_id: Unique recording ID
    :param recording_info: Recording Information Dictionary (new fields determined in this thread tracked here, eventually written to CSV file)
    :param progress: Rich Progress Bar (display bar)
    :param task: Rich Progress Task (updates display bar)
    :return: New and complete Recording Info dictionary
    """
    params = {"recordingId": recording_id, "max": 100}
    response = get_wrapper('recordingReport/accessDetail', token, params)

    if response:
        # A list of access entries is returned, with the final one being the most 'recent'
        audit_report_details = response['items'][-1]

        # Add last accessed time to recording details (use more readable date format)
        parsed_datetime = datetime.fromisoformat(audit_report_details['accessTime'][:-1])
        recording_info['accessTime'] = parsed_datetime.strftime("%m/%d/%y")

        progress.console.print(f'- [green]Recording Processed[/]: {recording_info["topic"]}', highlight=False)
    else:
        # Unable to get access timestamp, set to 'unknown'
        recording_info['accessTime'] = "Unknown"

    # Signify thread is complete, recording is 'processed'
    progress.update(task, advance=1)


def get_recordings_data(token, site_url, progress):
    """
    Extract bulk of recording metadata (access history is found in get_audit_report method), return recording metadata for all recordings in a site (https://developer.webex.com/docs/api/v1/recordings/list-recordings-for-an-admin-or-compliance-officer)
    :param token: Webex OAuth token
    :param site_url: Webex Site URL
    :param progress: Rich Progress Bar (used for display)
    :return:
    """
    site_recordings_processed = []
    site_recordings_raw = []

    # Because Admin Recordings API only works in increments of 30 days, we need to iteratively get recordings in 30
    # day cycles starting from the current day

    if config.TIME_PERIOD < MAX_DAYS:
        days_ago = config.TIME_PERIOD

        # Display
        task = progress.add_task(f"Gathering Recordings ({config.TIME_PERIOD} Days", total=1, transient=True)
    else:
        days_ago = MAX_DAYS

        # Display
        total_periods = math.ceil(config.TIME_PERIOD / float(MAX_DAYS))
        task = progress.add_task(f"Gathering Recordings ({config.TIME_PERIOD} Days: {total_periods} '{MAX_DAYS} Day' Periods)", total=total_periods, transient=True)

    days_remaining = config.TIME_PERIOD
    current_time = datetime.now(pytz.timezone("UTC"))
    while days_remaining > 0:
        # Calculate time period for getting reports - ISO format (gets the next 'MAX_DAYS' interval)
        from_time_iso, to_time_iso = calculate_iso_timestamps(current_time, days_ago)

        # Get site recordings for the time interval
        params = {"max": "100", "siteUrl": site_url, "from": from_time_iso, "to": to_time_iso}
        response = get_wrapper('admin/recordings', token, params)

        # Append recordings returned in interval (across pages) to larger list
        if response:
            recordings_in_interval = response['items']
            site_recordings_raw += recordings_in_interval

        days_remaining -= MAX_DAYS
        if days_remaining < 0:
            days_remaining = 0

        # Set new starting point for time calculations
        current_time -= timedelta(days=MAX_DAYS)

        # Signify interval is complete, recording is 'gathered'
        progress.update(task, advance=1)

    # Remove any duplicate recordings added across intervals (edge case of recordings right on the border of an
    # interval)
    unique_ids = set()
    site_recordings_raw_no_duplicates = []

    for recording in site_recordings_raw:
        if recording['id'] not in unique_ids:
            site_recordings_raw_no_duplicates.append(recording)
            unique_ids.add(recording['id'])

    # Progress display for recordings
    task = progress.add_task("Process Recordings", total=len(site_recordings_raw_no_duplicates), transient=True)

    threads = []
    for recording in site_recordings_raw_no_duplicates:
        # Build recording meta data dictionary
        recording_info = {"site_url": site_url, "createTime": recording["createTime"], "topic": recording['topic'],
                          "hostDisplayName": recording['hostDisplayName'], "sizeMegaBytes": '',
                          "format": recording['format'], "durationMinutes": '',
                          "serviceType": recording["serviceType"]}

        # Convert create time to more readable date format
        parsed_datetime = datetime.fromisoformat(recording_info['createTime'][:-1])
        recording_info['createTime'] = parsed_datetime.strftime("%m/%d/%y")

        # Convert duration in seconds to minutes (easier to read), convert bytes to MB (easier to read)
        recording_info["durationMinutes"] = round(recording['durationSeconds'] / 60.0)
        recording_info['sizeMegaBytes'] = round(recording['sizeBytes'] / (1024 ** 2), 2)

        # Get site audit report (for last accessed field) - spawn background thread to process 100 recordings at a
        # time - speeds up large scale workloads
        thread = threading.Thread(target=get_audit_report,
                                  args=(token, recording['id'], recording_info, progress, task))
        threads.append(thread)

        # Append to larger recording details list
        site_recordings_processed.append(recording_info)

    # Start all threads
    for t in threads:
        t.start()

    # Wait for all threads to finish
    for t in threads:
        t.join()

    return site_recordings_processed


def populate_df(df, recording_data):
    """
    Populate Pandas DataFrame with all recording information gathered from each site (the sum is eventually written to a CSV file)
    :param df: Existing Pandas Dataframe (represent current state of information from processed sites)
    :param recording_data: List of recording metadata gathered from Webex API
    :return: Pandas DataFrame with new recording information from the site appended
    """
    rows = []
    for recording in recording_data:
        # Create new row in CSV, append to rows
        df_row = {'Site URL': recording['site_url'], 'Recording Name': recording['topic'],
                  'Host Display Name': recording['hostDisplayName'],
                  'Date Created': recording['createTime'],
                  'Last Accessed': recording['accessTime'],
                  'Duration (minutes)': recording['durationMinutes'],
                  'Recording Size (megabytes)': recording['sizeMegaBytes'],
                  'Recording Format': recording['format'], 'Service Type': recording['serviceType']}

        rows.append(pd.DataFrame([df_row]))

    # Append new row(s)
    df = pd.concat(rows + [df], ignore_index=True, sort=False)
    return df


def generate_recording_report(token):
    """
    Main generate report workflow. Generate a Webex Recording report across a single (or multiple) sites. The report
    contains recording metadata like (Recording Name, Last Accessed, etc.) for each recording, and is written to a CSV file
    :param token:  Webex OAuth token
    """
    console.print(Panel.fit("Select Webex Site URLs", title="Step 2"))

    # Get all "Sites" user has access to
    sites = get_site_list(token)

    if not sites:
        console.print('[red]No sites found, exiting...[/]')
        return

    # Build site URL list (isolate default site)
    all_site_urls = []
    default_site_url = ""
    for site in sites:
        all_site_urls.append(site['siteUrl'])

        if site['default']:
            default_site_url = site['siteUrl']

    console.print(f"Found the following Site URLS for this user: {all_site_urls}")

    # Prompts, Controls which sites we process (all sites, default site, select sites)
    match_behavior = IntPrompt.ask(
        "Select which sites you would like to run the report for. Options are explained in the README.\n1.) All\n2.) "
        f"Default Site ([green]{default_site_url}[/])\n3.) Site List ([yellow]config.py[/])\nSelection",
        choices=["1", "2", "3"],
        show_choices=False, show_default=False)

    if match_behavior == 1:
        # Process all sites
        report_site_urls = all_site_urls
    elif match_behavior == 2:
        # Process default only
        report_site_urls = [default_site_url]
    else:
        # Process provided list (sanity check all provided sites are accessible from this user)
        all_present = all(element in all_site_urls for element in config.SITE_LIST)

        if len(config.SITE_LIST) == 0 or not all_present:
            console.print(
                f"[red]Error: the logged in user does not have access to one or more sites in {config.SITE_LIST}[/]")
            return
        else:
            report_site_urls = config.SITE_LIST

    # Sanity check time period is greater than or equal to 0
    if config.TIME_PERIOD <= 0:
        console.print(f"[red]Error: time period ({config.TIME_PERIOD}) must be > 0 days![/]")
        return

    console.print(Panel.fit("Generate Webex Recording Report ", title="Step 3"))

    # Define Recording Report Dataframe
    df_recording_report = pd.DataFrame()

    total_sites = len(report_site_urls)
    with Progress() as progress:
        overall_progress = progress.add_task("Overall Progress", total=total_sites, transient=True)
        counter = 1

        for site in report_site_urls:
            progress.console.print(
                "Processing Site: [blue]'{}'[/] ({} of {})".format(site, str(counter), total_sites))

            # Retrieve recording info for each site
            site_recording_data = get_recordings_data(token, site_url=site, progress=progress)

            ### Recording Report ###
            if len(site_recording_data) == 0:
                progress.console.print(f"- [red]Unable to find any recording data for site `{site}` during the time interval.[/]")
            else:
                progress.console.print(f"[green]Total[/]: Found and processed {len(site_recording_data)} recordings!")

                # Populate DF with report info
                df_recording_report = populate_df(df_recording_report, site_recording_data)

            counter += 1
            progress.update(overall_progress, advance=1)

            # Cleanup Intermediate progress displays (ignore first task -> overall task)
            task_ids = progress.task_ids
            task_ids.pop(0)
            for task_id in task_ids:
                progress.remove_task(task_id)

    console.print(Panel.fit("Saving File", title="Step 4"))

    # Datestamp for filename
    current_date = datetime.now()
    date_string = current_date.strftime("%m-%d-%Y_%H-%M-%S")

    # Create CSV report (report folder)
    report_file = f"recording_report_{date_string}.csv"
    df_recording_report.to_csv(os.path.join(report_folder, report_file), index=False)

    console.print(f'New report created: `[yellow]{report_file}[/]`', highlight=False)


def main():
    """
    Main method, determine if current Webex Token is valid (refresh if possible/necessary), kickstart report generation workflow
    """
    console.print(Panel.fit("Webex Recording Report"))

    # If token file already exists, extract existing tokens
    if os.path.exists(tokens_file):
        with open(tokens_file) as f:
            tokens = json.load(f)
    else:
        tokens = None

    console.print(Panel.fit("Obtain Webex API Tokens", title="Step 1"))

    # Determine relevant route to obtain a valid Webex Token. Options include:
    # 1. Full OAuth (both primary and refresh token are expired)
    # 2. Simple refresh (only the primary token is expired)
    # 3. Valid token (the existing primary token is valid)
    if tokens is None or time.time() > (
            tokens['expires_at'] + (tokens['refresh_token_expires_in'] - tokens['expires_in'])):
        # Both tokens expired, run the OAuth Workflow
        console.print("[red]Both tokens are expired, we need to run OAuth workflow... See README.[/]")
        sys.exit(0)
    elif time.time() > tokens['expires_at']:
        # Generate a new token using the refresh token
        console.print("Existing primary token [red]expired[/]! Using refresh token...")
        tokens = refresh_token(tokens)
        generate_recording_report(tokens['access_token'])
    else:
        # Use existing valid token
        console.print("Existing primary token is [green]valid![/]")
        generate_recording_report(tokens['access_token'])


if __name__ == "__main__":
    main()
