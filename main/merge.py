import re
import csv
import json
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email_merge_tool.utils import log_email_status, log_error, validate_recipient_data, format_template_content, sanitize_email_address

# 1. Loading Email Template
def load_email_template(template_path: str) -> str:
    """
    Loads the email template from the specified file path with improved error handling.
    """
    try:
        with open(template_path, 'r', encoding='utf-8') as file:
            return file.read()
    except FileNotFoundError:
        raise ValueError(f"Template file not found: {template_path}")
    except PermissionError:
        raise PermissionError(f"Permission denied while accessing the file: {template_path}")
    except Exception as e:
        raise RuntimeError(f"An error occurred while loading the template: {e}")

# 2. Extracting Parameters from Template
def extract_template_parameters(template_content: str) -> list:
    """
    Extracts placeholders (e.g., {name}, {email}) from the template content.
    """
    pattern = r'{(\w+)}'
    placeholders = re.findall(pattern, template_content)
    return list(set(placeholders))  # Return unique placeholders

# 3. Loading Data for the Merge
def load_recipient_data(data_source) -> list:
    """
    Loads recipient data from a data source (CSV, JSON, or list of dictionaries).
    """
    recipients = []
    if isinstance(data_source, str):
        if data_source.endswith('.csv'):
            try:
                with open(data_source, 'r', newline='', encoding='utf-8') as csvfile:
                    reader = csv.DictReader(csvfile)
                    recipients = [row for row in reader]
            except Exception as e:
                raise RuntimeError(f"Error loading CSV file: {e}")
        elif data_source.endswith('.json'):
            try:
                with open(data_source, 'r', encoding='utf-8') as jsonfile:
                    recipients = json.load(jsonfile)
            except Exception as e:
                raise RuntimeError(f"Error loading JSON file: {e}")
        else:
            raise ValueError("Unsupported file format. Use CSV or JSON.")
    elif isinstance(data_source, list):
        recipients = data_source
    else:
        raise TypeError("Data source must be a file path (CSV or JSON) or a list of dictionaries.")
    
    return recipients

# 4. Creating a Connection to Gmail or Outlook using SMTP_SSL
def create_email_connection(service: str, credentials: dict):
    """
    Creates a secure SMTP connection to Gmail or Outlook using SMTP_SSL.
    """
    try:
        if service == 'gmail':
            smtp_server = 'smtp.gmail.com'
            port = 465
        elif service == 'outlook':
            smtp_server = 'smtp-mail.outlook.com'
            port = 465
        else:
            raise ValueError("Unsupported email service. Use 'gmail' or 'outlook'.")

        # Use SMTP_SSL for a secure connection
        connection = smtplib.SMTP_SSL(smtp_server, port)
        connection.login(credentials.get('email', credentials['username']), credentials['password'])
        return connection
    except Exception as e:
        log_error(e, context="Email Connection")
        raise RuntimeError(f"Failed to create email connection: {e}")

# 5. Sending the Email
def send_email(connection, recipient: dict, email_content: str, is_html=False):
    """
    Sends an email using the provided connection, recipient details, and email content.
    Supports plain text or HTML emails.
    """
    try:
        msg = MIMEMultipart()
        msg['From'] = connection.user
        msg['To'] = sanitize_email_address(recipient['email'])
        msg['Subject'] = recipient.get('subject', 'No Subject')  # Optional subject handling

        # Attach email content as plain text or HTML
        if is_html:
            msg.attach(MIMEText(email_content, 'html'))
        else:
            msg.attach(MIMEText(email_content, 'plain'))
        
        connection.sendmail(connection.user, recipient['email'], msg.as_string())
        return "success"
    except Exception as e:
        log_error(e, context=f"Sending Email to {recipient['email']}")
        return f"Failed to send email to {recipient['email']}: {e}"

# 6. Batch Processing for Mail Merge
def mail_merge(template_path: str, data_source, service: str, credentials: dict, is_html=False):
    """
    Main function that performs mail merge. Logs the status of each email sent.
    """
    # Load template
    template_content = load_email_template(template_path)

    # Extract placeholders
    template_params = extract_template_parameters(template_content)

    # Load recipient data
    recipients = load_recipient_data(data_source)

    try:
        # Create email connection
        with create_email_connection(service, credentials) as connection:
            for recipient in recipients:
                # Validate recipient data to ensure all required parameters are present
                missing_params = validate_recipient_data(recipient, template_params)
                if missing_params:
                    log_email_status(recipient, f"Skipping due to missing parameters: {missing_params}")
                    continue

                # Fill in the template with recipient data
                email_content = format_template_content(template_content, recipient)

                # Send email
                status = send_email(connection, recipient, email_content, is_html)
                log_email_status(recipient, status)
                print(f"Email sent to {recipient['email']} with status: {status}")
    except Exception as e:
        log_error(e, context="Mail Merge")
        raise RuntimeError(f"An error occurred during mail merge: {e}")
