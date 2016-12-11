#!/usr/bin/env python3

# gien - export Github issue tracker & wiki contents to local email storage
# Copyright (C) 2016 Jens John <jjohn@2ion.de>
# 
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from argparse               import ArgumentParser
from concurrent.futures     import ThreadPoolExecutor
from email.message          import Message
from email.mime.multipart   import MIMEMultipart
from email.mime.text        import MIMEText
from email.utils            import formatdate
from github                 import Github, GithubException
from hashlib                import md5
from mailbox                import mbox
from markdown               import markdown
from pygit2                 import clone_repository
from tempfile               import TemporaryDirectory
from tui                    import TUIProgressBar
import os
import sys

def hexhex(res):
    h = md5()
    h.update(res.encode('utf-8'))
    return h.hexdigest()

def die(*args):
    print("[error]", *args, file=sys.stderr)
    sys.exit(1)

def tick():
    sys.stdout.write(".")
    sys.stdout.flush()

def get_options():
    ap = ArgumentParser(description="Export Github issue trackers to local email storage")
    ap.add_argument("-u", "--user",
            default=None,
            help="Github API authentication: user")
    ap.add_argument("-p", "--password",
            default=None,
            help="Github API authentication: password")
    ap.add_argument("-r", "--repository",
            default="2ion/gien",
            help="Github repository name the issue tracker of which shall be exported. Example: 2ion/gien")
    ap.add_argument("-i", "--issues",
            default="all",
            choices=["all", "open", "closed"],
            help="Filter issues by state. Defaults to all.")
    ap.add_argument("-o", "--output",
            default="output.mbox",
            help="Path to the output mbox file.")
    ap.add_argument("-l", "--labels",
            action="store_true",
            default=False,
            help="If the issue has labels, add them to the email Subject: header. If the issue has been marked as closed, at a [CLOSED] label to the subject.")
    ap.add_argument("-W", "--archive-wiki",
            default=False,
            action="store_true",
            help="Enable wiki archiving.")
    ap.add_argument("-I", "--archive-issues",
            default=False,
            action="store_true",
            help="Enable issue archiving.")
    ap.add_argument("-t", "--threads",
            default=2,
            type=int,
            help="Number of worker threads")
    r = ap.parse_args()
    if not (r.user and r.password and r.repository):
        die("Missing option: --user, --password and --repository are required.")
    return r

def fetch_rate_limit(api):
    limit = api.get_rate_limit()
    return "{}/{} requests, last reset at {} (UTC)".format(limit.rate.remaining,
            limit.rate.limit, limit.rate.reset)

def fetch_data(opts):
    data = []
    repo = None
    try:
        api = Github(opts.user, opts.password)
        print("Rate limit:", fetch_rate_limit(api), file=sys.stderr)
        repo = api.get_repo(opts.repository)
        issues = repo.get_issues(state=opts.issues, direction="asc")
        data = [{
            "issue"    : i,
            "comments" : i.get_comments(), # ordered by ascending id
            "labels"   : i.get_labels() } for i in issues ] # ordered by created asc
    except GithubException as e:
        die("Github API exception", e)
    return data, repo

def h_message_id(repo, issueid, commentid):
    return "<{}/issues/{}/{}@github.com>".format(repo, issueid, commentid)

def h_from(obj):
    return "{} <{}@noreply.github.com>".format(obj.user.login, obj.user.login)

def h_date(obj):
    return formatdate(obj.created_at.timestamp())

def h_subject(obj, in_reply=True):
    return ("Re: {}" if in_reply else "{}").format(obj.title)

def h_to(r):
    return "{} <{}@noreply.github.com>".format(r.full_name, r.name)

def render_message(body, **kwargs):
    m = MIMEMultipart('alternative')
    for k,v in kwargs.items():
        m[k.replace("_", "-")] = v
    try:
        m.attach(MIMEText(markdown(body), 'html'))
        m.attach(MIMEText(body, 'plain'))
    except:
        pass
    return m

def make_thread(tup):
    (opts, r, o) = tup

    common_Subject = "{}".format(o['issue'].title)
    if opts.labels:
        for label in o['issue'].labels:
            common_Subject += " [{}]".format(label.name)
        if o['issue'].closed_at:
            common_Subject += " [CLOSED]"
    common_Subject_Re = "Re: " + common_Subject

    common_To = h_to(r)

    thread = [ render_message(o['issue'].body,
                Subject=common_Subject,
                From=h_from(o['issue']),
                To=common_To,
                Date=h_date(o['issue']),
                Message_ID=h_message_id(r.full_name, o['issue'].id, 0)) ]
    
# Mimic the behaviour of the Github email notification system
    common_root = thread[-1]['Message-ID']

    for comment in o['comments']:
        thread.append(render_message(comment.body,
            Subject=common_Subject_Re,
            From=h_from(comment),
            To=common_To,
            Date=h_date(comment),
            Message_ID=h_message_id(r.full_name, o['issue'].id, comment.id),
            In_Reply_To=common_root,
            References=common_root))

    return thread

def thread_wiki(repo):
    h_from = "wiki@noreply.github.com".format(repo.full_name)
    to = h_to(repo)
    root_msgid = "{}@wiki".format(hexhex(repo.full_name))

    thread = []

    with TemporaryDirectory() as DIR:
        print("Cloning wiki...")
        clone_repository(repo.clone_url.replace(".git",".wiki"), DIR)
        for r,d,f in os.walk(DIR):
            if r.find(".git") > -1:
                continue
            for ff in f:
                path = "{}/{}".format(r,ff)
                print("Inspecting {}".format(path))
                if ff.endswith(".md"):
                    with open(path, "r") as FILE:
                        body = FILE.read()
                        date = formatdate()
                        subject = "[WIKI] {}".format(ff[:-3])
                        if len(thread)>0:
                            msgid = "{}@{}.wiki".format(hexhex(path), repo.name)
                            msg = render_message(body,
                                    Subject     = subject,
                                    From        = h_from,
                                    Message_ID  = msgid,
                                    To          = to,
                                    In_Reply_To = root_msgid,
                                    References  = root_msgid,
                                    Date        = date)
                        else:
                            msgid = root_msgid
                            msg = render_message(body,
                                    Subject    = subject,
                                    From       = h_from,
                                    Message_ID = msgid,
                                    To         = to,
                                    Date       = date)
                        thread.append(msg)
    return thread

def main():
    opts = get_options()
    data, repo = fetch_data(opts)

    mb = mbox(opts.output)
    mb.lock()

    if opts.archive_issues:
        with TUIProgressBar("Issues", len(data)) as bar:
            with ThreadPoolExecutor(max_workers = opts.threads) as Exec:
                for thread in Exec.map(make_thread, [ (opts, repo, issue,) for
                    issue in data ]):
                    bar.tick()
                    for msg in thread:
                        mb.add(msg)

    if opts.archive_wiki:
        for msg in thread_wiki(repo):
            mb.add(msg)

    mb.flush()
    mb.unlock()
    mb.close()

    return 0

if __name__ == "__main__":
    sys.exit(main())