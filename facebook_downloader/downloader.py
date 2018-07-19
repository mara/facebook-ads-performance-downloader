import datetime
import errno
import heapq
import json
import logging
import re
import sqlite3
import sys
import threading
import time
import timeit
import typing
from functools import wraps
from pathlib import Path
from typing import Generator, List, Union

from facebook_downloader import config
from facebookads.adobjects import user, adaccount, adsinsights
from facebookads.adobjects.adreportrun import AdReportRun
from facebookads.api import FacebookAdsApi, FacebookRequestError

OUTPUT_FILE_VERSION = 'v1'


def download_data():
    """Initializes the FacebookAdsAPI, retrieves the ad accounts and downloads the data"""
    FacebookAdsApi.init(config.app_id(),
                        config.app_secret(),
                        config.access_token())
    ad_accounts = _get_ad_accounts()
    target_accounts = list(filter(None, config.target_accounts().split(',')))
    if len(target_accounts) > 0:
        logging.info('the app can see %s accounts but the configuration specified only %s target accounts: %s',
                     len(ad_accounts), len(target_accounts), ', '.join(target_accounts))
        ad_accounts = [ad_account for ad_account in ad_accounts if ad_account['account_id'] in config.target_accounts()]
        logging.info('after filtering %s accounts will be downloaded: %s', len(target_accounts),
                     ', '.join(target_accounts))
    download_data_sets(ad_accounts)


def download_data_sets(ad_accounts: [adaccount.AdAccount]):
    """Downloads the account structure and ad performance data sets for all ad accounts

    Args:
        ad_accounts: A list of all ad accounts to download.

    """
    download_account_structure(ad_accounts)
    download_ad_performance(ad_accounts)


def download_account_structure(ad_accounts: [adaccount.AdAccount]):
    """Downloads the Facebook Ads account structure to a csv.

    Args:
        ad_accounts: A list of all ad accounts to download.

    """
    db_name = Path('facebook-account-structure_{}.sqlite3'.format(OUTPUT_FILE_VERSION))
    filepath = ensure_data_directory(db_name)

    with sqlite3.connect(str(filepath)) as con:
        for ad_account in ad_accounts:
            for row in download_account_structure_per_account(ad_account):
                _upsert_account_structure(row, con)


def _upsert_account_structure(campaign_data, con: sqlite3.Connection):
    """Creates the campaign performance table if it does not exists and upserts the
    campaign data afterwards

    Args:
        campaign_data: A list of campaign information objects
        con: A sqlite database connection

    """
    con.execute("""
CREATE TABLE IF NOT EXISTS account_structure (
  ad_id       BIGINT   NOT NULL,
  ad          TEXT NOT NULL,
  ad_set_id   BIGINT   NOT NULL,
  ad_set      TEXT NOT NULL,
  campaign_id BIGINT   NOT NULL,
  campaign    TEXT NOT NULL,
  account_id  BIGINT   NOT NULL,
  account     TEXT NOT NULL,
  attributes  JSON,
  PRIMARY KEY (ad_id)
);""")
    con.execute("INSERT OR REPLACE INTO account_structure VALUES (?,?,?,?,?,?,?,?,?)",
                campaign_data)


def download_ad_performance(ad_accounts: [adaccount.AdAccount]):
    """Download the Facebook ad performance and upserts them
    into a sqlite database per account and day

    Args:
        ad_accounts: A list of all ad accounts to download.

    """
    job_list: List[JobQueueItem] = list()
    for ad_account in ad_accounts:
        # calculate yesterday based on the timezone of the ad account
        ad_account_timezone = datetime.timezone(datetime.timedelta(
            hours=float(ad_account['timezone_offset_hours_utc'])))
        last_date = datetime.datetime.now(ad_account_timezone).date() - datetime.timedelta(days=1)
        first_date = _first_download_date_of_ad_account(ad_account)

        # check for ad performance db on the first day the account
        current_date = last_date
        while current_date >= first_date:
            db_name = ensure_data_directory(
                Path("{date:%Y/%m/%d}/facebook/ad-performance-act_{account_id}.sqlite3"
                     .format(date=current_date,
                             account_id=ad_account['account_id'])))

            if (not db_name.is_file()
                    or (last_date - current_date).days <= int(config.redownload_window())):
                job_list.append(JobQueueItem(ad_account['account_id'], current_date, str(db_name)))
            current_date -= datetime.timedelta(days=1)

    process_single_day_jobs_concurrently(job_list, int(config.number_of_ad_performance_threads()))


def download_account_structure_per_account(ad_account: adaccount.AdAccount) \
        -> Generator[List, None, None]:
    """Downloads the Facebook Ads account structure for a specific account
    and transforms them to flat rows per ad

    Args:
        ad_account: An ad account to download.

    Returns:
        An iterator of campaign structure rows

    """
    campaign_data = get_campaign_data(ad_account)
    ad_set_data = get_ad_set_data(ad_account)
    ad_data = get_ad_data(ad_account)

    for ad_id, ad in ad_data.items():
        ad_set_id = ad['ad_set_id']
        ad_set = ad_set_data[ad_set_id]
        campaign_id = ad_set['campaign_id']
        campaign = campaign_data[campaign_id]

        attributes = {**campaign['attributes'],
                      **ad_set['attributes'],
                      **ad['attributes']}

        row = [ad_id,
               ad['name'],
               ad_set_id,
               ad_set['name'],
               campaign_id,
               campaign['name'],
               ad_account['account_id'],
               ad_account['name'],
               json.dumps(attributes)]

        yield row


def rate_limiting(func):
    """Wraps the function and applies an exponentially increasing sleep time
    if a rate limiting error occurs

    Args:
        func: A function that should be rate limited

    Returns:
        The result of the wrapped function

    """

    @wraps(func)
    def func_wrapper(*args, **kwargs):
        # Retry counter for rate limiting
        number_of_attempts = 0
        while True:
            try:
                return func(*args, **kwargs)
            except FacebookRequestError as e:
                if number_of_attempts < 7:
                    duration = 60 * 2 ** number_of_attempts
                    logging.warning(e.get_message())
                    logging.warning(e.api_error_message())
                    logging.info('Retry #{attempt} in {duration} seconds'.format(attempt=number_of_attempts,
                                                                                 duration=duration))
                    time.sleep(duration)
                    number_of_attempts += 1
                else:
                    raise

    return func_wrapper


@rate_limiting
def get_ad_data(ad_account: adaccount.AdAccount) -> {}:
    """Retrieves the ad data of the ad account as a dictionary

    Args:
        ad_account: An ad account for which to retrieve the ad data

    Returns:
        A dictionary with {ad_id: {'name': 1, 'ad_set_id': 2, 'attributes': {}}} format

    """
    logging.info('get ad data for account {}'.format(ad_account['account_id']))
    ads = ad_account.get_ads(
        fields=['id',
                'name',
                'adset_id',
                'adlabels'],
        params={'limit': 1000,
                'status': ['ACTIVE',
                           'PAUSED',
                           'ARCHIVED']})
    result = {}

    for ad in ads:
        result[ad['id']] = {'name': ad['name'],
                            'ad_set_id': ad['adset_id'],
                            'attributes': parse_labels(ad.get('adlabels', []))}
    return result


@rate_limiting
def get_ad_set_data(ad_account: adaccount.AdAccount) -> {}:
    """Retrieves the ad set data of the ad account as a dictionary

    Args:
        ad_account: An ad account for which to retrieve the ad set data

    Returns:
        A dictionary with {ad_set_id: {'name': 1,
                                       'campaign_id': 2,
                                       'attributes': {}}} format

    """
    logging.info('get ad set data for account {}'.format(ad_account['account_id']))
    ad_sets = ad_account.get_ad_sets(
        fields=['id',
                'name',
                'campaign_id',
                'adlabels'],
        params={'limit': 1000,
                'status': ['ACTIVE',
                           'PAUSED',
                           'ARCHIVED']})
    result = {}

    for ad_set in ad_sets:
        result[ad_set['id']] = {'name': ad_set['name'],
                                'campaign_id': ad_set['campaign_id'],
                                'attributes': parse_labels(
                                    ad_set.get('adlabels', []))}
    return result


@rate_limiting
def get_campaign_data(ad_account: adaccount.AdAccount) -> {}:
    """Retrieves the campaign data of the ad account as a dictionary

    Args:
        ad_account: An ad account for which to retrieve the campaign data

    Returns:
        A dictionary with {campaign_id: {'name': 1, 'attributes': {}}} format

    """
    logging.info('get campaign data for account {}'.format(ad_account['account_id']))
    campaigns = ad_account.get_campaigns(
        fields=['id',
                'name',
                'adlabels'],
        params={'limit': 1000,
                'status': ['ACTIVE',
                           'PAUSED',
                           'ARCHIVED']})
    result = {}

    for campaign in campaigns:
        result[campaign['id']] = {'name': campaign['name'],
                                  'attributes': parse_labels(
                                      campaign.get('adlabels', []))}
    return result


def get_account_ad_performance_for_single_day(ad_account: adaccount.AdAccount,
                                              single_date: datetime) -> adsinsights.AdsInsights:
    """Downloads the ad performance for an ad account for a given day
    https://developers.facebook.com/docs/marketing-api/insights

    Args:
        ad_account: An ad account to download.
        single_date: A single date as a datetime object

    Returns:
        A list containing dictionaries with the ad performance from the report

    """
    fields = ['date_start',
              'ad_id',
              'impressions',
              'actions',
              'spend',
              'action_values']

    params = {'action_attribution_windows': ['28d_click'],
              # https://developers.facebook.com/docs/marketing-api/insights/action-breakdowns
              'action_breakdowns': ['action_type'],
              # https://developers.facebook.com/docs/marketing-api/insights/breakdowns
              'breakdowns': ['impression_device'],
              'level': 'ad',
              'limit': 1000,
              'time_range': {'since': single_date.strftime('%Y-%m-%d'),
                             'until': single_date.strftime('%Y-%m-%d')},
              # By default only ACTIVE campaigns get considered.
              'filtering': [{
                  'field': 'ad.effective_status',
                  'operator': 'IN',
                  'value': ['ACTIVE',
                            'PAUSED',
                            'PENDING_REVIEW',
                            'DISAPPROVED',
                            'PREAPPROVED',
                            'PENDING_BILLING_INFO',
                            'CAMPAIGN_PAUSED',
                            'ARCHIVED',
                            'ADSET_PAUSED']}]}

    # https://developers.facebook.com/docs/marketing-api/insights/best-practices
    # https://developers.facebook.com/docs/marketing-api/asyncrequests/
    async_job = ad_account.get_insights(fields=fields, params=params, async=True)
    async_job.remote_read()
    while async_job[AdReportRun.Field.async_percent_completion] < 100 and async_job[AdReportRun.Field.async_status] != 'Job Failed':
        time.sleep(1)
        async_job.remote_read()
    time.sleep(1)

    ad_insights = async_job.get_result()

    return ad_insights


def ensure_data_directory(relative_path: Path = None) -> Path:
    """Checks if a directory in the data dir path exists. Creates it if necessary

    Args:
        relative_path: A Path object pointing to a file relative to the data directory

    Returns:
        The absolute path Path object

    """
    if relative_path is None:
        return Path(config.data_dir())
    try:
        path = Path(config.data_dir(), relative_path)
        # if path points to a file, create parent directory instead
        if path.suffix:
            if not path.parent.exists():
                path.parent.mkdir(exist_ok=True, parents=True)
        else:
            if not path.exists():
                path.mkdir(exist_ok=True, parents=True)
        return path
    except OSError as exception:
        if exception.errno != errno.EEXIST:
            raise


def parse_labels(labels: [{}]) -> {str: str}:
    """Extracts labels from a string.

    Args:
        labels: Labels in the form of
                [{"id": "1", "name": "{key_1=value_1}"},
                 {"id": "2", "name": "{key_2=value_2}"}]"'

    Returns:
            A dictionary of labels with {key_1 : value_1, ...} format

    """
    labels_dict = {}
    for label in labels:
        match = re.search("{([^=]+)=(.+)}", label['name'])
        if match:
            key = match.group(1).strip().lower().title()
            value = match.group(2).strip()
            labels_dict[key] = value
    return labels_dict


@rate_limiting
def _get_ad_accounts() -> [adaccount.AdAccount]:
    """Retrieves the ad accounts of the user whose access token was provided and
    returns them as a list.

    Returns:
        A list of ad accounts

    """
    system_user = user.User(fbid='me')
    ad_accounts = system_user.get_ad_accounts(fields=['account_id',
                                                      'name',
                                                      'created_time',
                                                      'timezone_offset_hours_utc'])
    return list(ad_accounts)


@rate_limiting
def _upsert_ad_performance(ad_insights: [adsinsights.AdsInsights], con: sqlite3.Connection):
    """Creates the ad performance table if it does not exists and upserts the
    ad insights data afterwards

    Args:
        ad_insights: A list of Insights objects
        con: A sqlite database connection

    """
    con.execute("""
CREATE TABLE IF NOT EXISTS ad_performance (
  date          DATE   NOT NULL,
  ad_id         BIGINT NOT NULL,
  device        TEXT   NOT NULL,
  performance   TEXT   NOT NULL,
  PRIMARY KEY (ad_id, device)
);""")
    con.executemany("INSERT OR REPLACE INTO ad_performance VALUES (?,?,?,?)",
                    _to_insight_row_tuples(ad_insights))


@rate_limiting
def _to_insight_row_tuples(ad_insights: [adsinsights.AdsInsights]) -> Generator[tuple, None, None]:
    """Transforms the Insights objects into tuples that can be directly inserted
    into the ad_performance table

    Args:
        ad_insights: A list of Insights objects for an ad on a specific day

    Returns:
        A list of tuples of ad performance data

    """

    @rate_limiting
    def get_ad_insight(field, ad_insight, default_value=[]):
        return ad_insight.get(field) or default_value

    for ad_insight in ad_insights:
        actions = get_ad_insight('actions', ad_insight)

        actions = [_floatify_values(action) for action in actions]

        action_values = get_ad_insight('action_values', ad_insight)
        action_values = [_floatify_values(action_value) for action_value in action_values]

        impressions = get_ad_insight('impressions', ad_insight, 0)
        spend = get_ad_insight('spend', ad_insight, 0.0)

        performance = {'impressions': int(impressions),
                       'spend': float(spend),
                       'actions': actions,
                       'action_values': action_values}

        ad_insight_tuple = (ad_insight['date_start'],
                            ad_insight['ad_id'],
                            get_ad_insight('impression_device', ad_insight, 'Unknown'),
                            json.dumps(performance))

        yield ad_insight_tuple


def _floatify(value: str) -> Union[str, float]:
    try:
        return float(value)
    except ValueError:
        return value


def _floatify_values(inp: {}) -> {}:
    return {key: _floatify(value) for key, value in inp.items()}


def _first_download_date_of_ad_account(ad_account: adaccount.AdAccount) -> datetime.date:
    """Finds the first date for which the ad account's performance should be
    downloaded by comparing the first download date from the configuration and
    the creation date of the account and returning the maximum of the two.

    Args:
        ad_account: An ad account to download

    Returns:
        The first date to download the performance data for

    """
    config_first_date = datetime.datetime.strptime(config.first_date(),
                                                   '%Y-%m-%d').date()
    if 'created_time' in ad_account:
        account_created_date = datetime.datetime.strptime(ad_account['created_time'],
                                                          "%Y-%m-%dT%H:%M:%S%z").date()
        return max(config_first_date, account_created_date)
    else:
        return config_first_date


class JobQueueItem:

    def __init__(self: 'JobQueueItem', ad_account_id: str, date: datetime.datetime,
                 db_name: str) -> None:
        self.ad_account_id: str = ad_account_id
        self.date: datetime.datetime = date
        self.db_name = db_name
        self.try_count: int = 0

    def __lt__(self: 'JobQueueItem', other: 'JobQueueItem') -> bool:
        # python heapq sorts lowest to highest

        if self.try_count > other.try_count:
            return True
        elif self.try_count < other.try_count:
            return False

        if self.date > other.date:
            return True

        return False

class RetryQueueItem:
    def __init__(self: 'RetryQueueItem', retry_at: datetime.datetime, job: JobQueueItem) -> None:
        self.retry_at: datetime.datetime = retry_at
        self.job = job

    def __lt__(self: 'RetryQueueItem', other: 'RetryQueueItem') -> bool:
        return self.retry_at < other.retry_at

class ThreadArgs:

    def __init__(self: 'ThreadArgs', job_list: typing.List[JobQueueItem]) -> None:
        self.job_list: typing.List[JobQueueItem] = job_list
        self.retry_queue: typing.List[RetryQueueItem] = list()
        self.jobs_left = len(job_list)

        # both cvs do no require the same mutex, but it is slightly more convenient than using
        # events and manually locking where needed
        self.job_list_cv: threading.Condition = threading.Condition()
        self.state_changed_cv: threading.Condition = threading.Condition()

        self.retry_queue_cv: threading.Condition = threading.Condition()
        self.logging_mutex: threading.Lock = threading.Lock()
        self.error_occured: bool = False
        self.done: bool = False


def process_single_day_jobs_concurrently(job_list: typing.List[JobQueueItem], n_threads: int) -> None:
    if n_threads < 1:
      raise ValueError('process_single_day_jobs_concurrently should have n_threads > 0')
    heapq.heapify(job_list)
    thread_args: ThreadArgs = ThreadArgs(job_list)
    # store the default API since the worker threads will change it
    default_api: FacebookAdsApi = FacebookAdsApi.get_default_api()

    thread_list: typing.List[threading.Thread] = list()
    thread: threading.Thread = threading.Thread(target = retry_thread_func, args = (thread_args, ))
    thread_list.append(thread)
    thread.start()
    for i in range(0, n_threads):
        thread = threading.Thread(target = job_thread_func, args = (thread_args,))
        thread_list.append(thread)
        thread.start()

    thread_args.state_changed_cv.acquire()
    try:
        while (not thread_args.error_occured) and (thread_args.jobs_left > 0):
            thread_args.state_changed_cv.wait()
    except:
        thread_args.error_occured = True
    finally:
        thread_args.done = True
        thread_args.state_changed_cv.release()
        # notify all waiting threads, so they can see that they are done
        # release -> aquire ordering matters due to potential deadlocking
        with thread_args.job_list_cv:
            thread_args.job_list_cv.notify_all()
        with thread_args.retry_queue_cv:
            thread_args.retry_queue_cv.notify()

    with thread_args.logging_mutex:
        logging.info('waiting for all threads to exit'.format(threading.get_ident()))
    for thread in thread_list:
        thread.join()

    # restore the default API in case something else needs it after this function
    FacebookAdsApi.set_default_api(default_api)

    if thread_args.error_occured:
        sys.exit(1)

def job_thread_func(args: ThreadArgs) -> None:
    job: typing.Optional[JobQueueItem]
    # Api objects do not seem thread safe at all, create on per thread and nuke the default for
    # good measure
    api: FacebookAdsApi = FacebookAdsApi.init(config.app_id(),
                                              config.app_secret(),
                                              config.access_token())
    FacebookAdsApi.set_default_api(None)
    # No need to lock since this can only change to True, so  worst case this does one extra
    # iteration. Also assignment / reading of booleans should be atomic anyway.
    while not args.done:

        job = get_job_from_queue(args)
        if job is None:
            continue

        process_job(args, job, api)

    log(logging.info, args.logging_mutex, ['thread {0} exited'.format(threading.get_ident())])

def get_job_from_queue(args: ThreadArgs) -> typing.Optional[JobQueueItem]:
    with args.job_list_cv:
        # wait for a job to become available
        while len(args.job_list) <= 0:
            args.job_list_cv.wait()
            # check if everything is done in order to avoid infinite loops when no new
            # jobs are added to the queue
            if (args.done):
                return None
        return heapq.heappop(args.job_list)

def process_job(args: ThreadArgs, job: JobQueueItem, api: FacebookAdsApi) -> None:
    account_id: str = job.ad_account_id
    date_str: str = job.date.strftime('%Y-%m-%d')
    job.try_count += 1
    job_info_str: str = 'act_{ad_account_id} on {single_date}'.format(ad_account_id=account_id,
                                                                      single_date=date_str)
    log(logging.info, args.logging_mutex, ['download Facebook ad performance of {job}'
            ' - attempt #{attempt}'.format(job = job_info_str, attempt = job.try_count)])

    # platform specific timer
    start = timeit.default_timer()

    request_error_occured: bool = False
    error_occured: bool = False
    error_msg: typing.List[str] = list()
    ad_insights: adsinsights.AdsInsights
    try:
        ad_account = adaccount.AdAccount('act_' + account_id, api = api)
        ad_insights = get_account_ad_performance_for_single_day(ad_account, job.date)
        with sqlite3.connect(job.db_name) as con:
            _upsert_ad_performance(ad_insights, con)

        end = timeit.default_timer()

        log(logging.info, args.logging_mutex, ['finished download Facebook ad performance of {job}'
        ' in {time}s - attempt #{attempt}'.format(job = job_info_str,
                                                  time=round(end - start, 2),
                                                  attempt = job.try_count)])

        with args.state_changed_cv:
            args.jobs_left -= 1
            if args.jobs_left == 0:
                args.state_changed_cv.notify()

    except FacebookRequestError as e:
        request_error_occured = True
        error_msg.append(e.get_message())
        error_msg.append(e.api_error_message())
    except Exception as e:
        error_occured = True
        error_msg.append(str(e))

    if request_error_occured:
        if job.try_count < 8:
            duration: int = 60 * 2 ** (job.try_count - 1)
            retry_at: datetime.datetime = datetime.datetime.now() + datetime.timedelta(
                    seconds = duration)
            retry_msg: str = 'retrying {job} in {duration} seconds - attempt #{attempt}'.format(
                    job = job_info_str, attempt = job.try_count, duration = duration)
            error_msg.append(retry_msg)
            with args.retry_queue_cv:
                heapq.heappush(args.retry_queue, RetryQueueItem(retry_at, job))
                args.retry_queue_cv.notify_all()
            log(logging.warning, args.logging_mutex, error_msg)
            return
        else:
            error_occured = True
            error_msg.append('download of {job} failed too many times'.format(job = job_info_str))

    if error_occured:
        log(logging.error, args.logging_mutex, error_msg)

        with args.state_changed_cv:
            # technically does not require locking but it is needed for the notify to work
            # so might as well put this in scope
            args.error_occured = True
            args.done = True
            args.state_changed_cv.notify()

        return


def retry_thread_func(args: ThreadArgs) -> None:
    # locking outside the main loop is fine here, since wait will relinquish the lock
    with args.retry_queue_cv:
        while not args.done:
            wait_timeout: typing.Optional[float] = None
            if len(args.retry_queue) > 0:
                now: datetime.datetime = datetime.datetime.now()
                top: typing.Optional[RetryQueueItem] = args.retry_queue[0]
                # duplicate check, but this prevents mutex contention when it is not required
                if (not top is None) and (now >= top.retry_at):
                    # note: this will not deadlock since none of the other code locks
                    # retry_queue_cv and job_list_cv nested in reverse order
                    with args.job_list_cv:
                        while (not top is None) and (now >= top.retry_at):
                            current_job: JobQueueItem = heapq.heappop(args.retry_queue).job
                            heapq.heappush(args.job_list, current_job)
                            if len(args.retry_queue) > 0:
                                top = args.retry_queue[0]
                            else:
                                top = None
                        args.job_list_cv.notify()

                if not top is None:
                    wait_timeout = (top.retry_at - now).total_seconds()

            args.retry_queue_cv.wait(wait_timeout)

def log(log_func: typing.Callable[[str], None], logging_mutex: threading.Lock,
        log_strs: typing.List[str]):
    with logging_mutex:
        for log_str in log_strs:
            log_func(log_str)
