"""Recognizing hostile requests.

Two kinds are distinguished:

* scanner probes: requests for software this site doesn't run (WordPress,
  PHP, .env/.git files, admin panels of other products). Bots send these to
  every server on the internet; they are logged, but they are not aimed at us.
* attack payloads: SQL injection, script injection, path traversal, template
  or command injection, Log4Shell strings and similar. Sent to one of this
  site's real pages (or in a form field), they mean someone is trying this
  site specifically.
"""

from __future__ import annotations

import re
from urllib.parse import unquote_plus

SCANNER_PATH = re.compile(
    r"""
    \.(?:php\d?|phtml|asp|aspx|ashx|jsp|jspa|do|action|cgi|pl|sh|bak|old|orig|save|swp|sql|sqlite|db
         |mdb|ini|conf|cfg|env|log|tar|tgz|gz|zip|rar|7z|pem|key|crt|yml|yaml|DS_Store)(?:$|[/?])
    | /\.(?:env|git|svn|hg|aws|ssh|docker|vscode|idea|htaccess|htpasswd|npmrc|kube|config)
    | /(?:wp-|wordpress|xmlrpc|wlwmanifest|phpmyadmin|pma|myadmin|mysql|adminer|cgi-bin|vendor/
        |phpunit|actuator|solr|jenkins|manager/html|console|boaform|HNAP1|owa/|ecp/|autodiscover
        |remote/|cf_scripts|druid|nacos|geoserver|telescope|_ignition|horde|webmail|roundcube
        |server-status|server-info|jmx-console|invoker|struts|setup\.cgi|shell|cmd|eval-stdin
        |sftp-config|config\.json|credentials|backup|dump|\.well-known/(?!acme-challenge|security\.txt))
    """,
    re.IGNORECASE | re.VERBOSE,
)

PAYLOADS = [
    ("sql_injection", re.compile(
        r"(?:\bunion\b[\s/*+]+(?:all[\s/*+]+)?select\b|\bselect\b.+\bfrom\b.+\bwhere\b|'\s*or\s*'?\d+'?\s*=\s*'?\d"
        r"|\bor\s+1\s*=\s*1\b|\b(?:pg_)?sleep\s*\(\s*\d|\bbenchmark\s*\(|\bwaitfor\s+delay\b|information_schema"
        r"|\bxp_cmdshell\b|;\s*(?:drop|delete|insert|update|truncate)\s|'\s*(?:--|#|/\*)|\bextractvalue\s*\("
        r"|\bupdatexml\s*\()",
        re.IGNORECASE)),
    ("script_injection", re.compile(
        r"<\s*(?:script|iframe|svg|img|body|object|embed)\b|javascript\s*:|\bon(?:error|load|mouseover|focus)\s*="
        r"|\balert\s*\(|document\.cookie|<\s*/\s*script",
        re.IGNORECASE)),
    ("path_traversal", re.compile(r"(?:\.\./|\.\.\\|%2e%2e|\x00|%00|/etc/(?:passwd|shadow)|win\.ini|boot\.ini)",
                                  re.IGNORECASE)),
    ("template_injection", re.compile(r"\{\{.*\}\}|\{%.*%\}|\$\{(?!jndi).*\}|#\{.*\}")),
    ("log4shell", re.compile(r"\$\{\s*(?:jndi|lower|upper|env|sys|::-)", re.IGNORECASE)),
    ("command_injection", re.compile(
        r"(?:[;|&`]|\$\()\s*(?:cat|ls|id|whoami|uname|wget|curl|bash|sh|nc|ping|powershell|cmd)\b",
        re.IGNORECASE)),
    ("ssrf_probe", re.compile(r"169\.254\.169\.254|metadata\.google\.internal|file://|gopher://|dict://",
                              re.IGNORECASE)),
]


def is_scanner_path(path: str) -> bool:
    return bool(SCANNER_PATH.search(path))


def find_payload(*values: str | None) -> tuple[str, str] | None:
    """Return (kind, offending text) for the first attack payload found, else None."""
    for value in values:
        if not value:
            continue
        decoded = value
        for _ in range(2):  # catch double URL-encoding
            decoded = unquote_plus(decoded)
        for kind, pattern in PAYLOADS:
            for candidate in (value, decoded):
                match = pattern.search(candidate)
                if match:
                    start = max(match.start() - 40, 0)
                    return kind, candidate[start : match.end() + 40]
    return None
