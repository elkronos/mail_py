# Email Merge Tool

Email Merge Tool is a Python package that simplifies the process of sending bulk emails using customizable templates. It supports Gmail and Outlook and allows you to send either plain text or HTML emails using a mail merge.

## Features

- Load customizable email templates (supports placeholders like `{name}`, `{email}`).
- Send emails using Gmail or Outlook.
- Send bulk emails to recipients from CSV or JSON files.
- Log the status of each email sent.
- Simple to use via the command line or within Python code.

## Installation

To install the package:

```bash
pip install .


## Usage

### 1. Using the `mail_merge` function in Python:

```python
from email_merge_tool import mail_merge

# Define your email credentials
credentials = {
    'username': 'your-username',  # If different from email
    'email': 'your-email@example.com',
    'password': 'your-password'
}

# Run mail merge
mail_merge(
    template_path='email_merge_tool/templates/email_template.txt',
    data_source='email_merge_tool/data/recipients.csv',  # Could also be a JSON file or a list of dictionaries
    service='gmail',  # or 'outlook'
    credentials=credentials,
    is_html=True  # Set to False for plain text emails
)


