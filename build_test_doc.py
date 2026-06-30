"""Create the synthetic test document (the one testing_run.py --integration
builds) and SHARE it with you, leaving it in place so you can open and inspect
exactly what the harness feeds the pipeline.

    .venv\\Scripts\\python.exe build_test_doc.py [email]

Prints the Google Docs link. The doc is owned by the service account and
shared with you as an editor.
"""
from __future__ import annotations

import sys

from app.auth import drive_service
from testing_run import TEST_DOC_FOOTER, TEST_DOC_PARAS, make_test_doc

DEFAULT_EMAIL = "laurence.peplow@transportenvironment.org"
TITLE = "Report Checker - synthetic test document"


def main() -> None:
    email = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_EMAIL
    doc_id = make_test_doc(TITLE)
    link = f"https://docs.google.com/document/d/{doc_id}/edit"
    try:
        drive_service().permissions().create(
            fileId=doc_id,
            body={"type": "user", "role": "writer", "emailAddress": email},
            sendNotificationEmail=True, fields="id").execute()
        print(f"Created and shared with {email} (check your inbox / 'Shared with me').")
    except Exception as exc:  # noqa: BLE001
        print(f"Created, but could NOT share automatically ({exc}).")
        print("It's owned by the service account - share it from there, or open the link if you have access.")
    print(f"\n{link}\n")
    print(f"It contains {len(TEST_DOC_PARAS)} paragraphs (front matter, H1 sections, an em-dash")
    print("divider, a 17-word sentence, 'Transport & Environment', a bold sentence, a")
    print(f"hyperlink, a Methodology section) and a page footer: {TEST_DOC_FOOTER!r}")


if __name__ == "__main__":
    main()
