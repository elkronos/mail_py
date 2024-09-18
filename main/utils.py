import logging

# Configure the logging module to log info messages and above to a file
logging.basicConfig(
    filename='email_log.txt',  # Log file
    level=logging.INFO,  # Log info messages and above
    format='%(asctime)s - %(levelname)s - %(message)s',  # Log format
    filemode='a'  # Append mode, to avoid overwriting the file
)

def log_email_status(recipient: dict, status: str):
    """
    Logs the status of an email for a recipient.
    """
    logging.info(f"Recipient: {recipient.get('email', 'unknown')}, Status: {status}")

def log_error(exception: Exception, context: str = ""):
    """
    Logs detailed error messages including context information and exception details.
    """
    error_message = f"Error occurred in {context}: {str(exception)}"
    logging.error(error_message)

def validate_recipient_data(recipient: dict, required_fields: list) -> list:
    """
    Validates if the recipient data contains all required fields.
    
    :param recipient: Dictionary containing recipient information.
    :param required_fields: List of required fields (e.g., ['email', 'name']).
    :return: List of missing fields, or empty list if all are present.
    """
    missing_fields = [field for field in required_fields if field not in recipient]
    return missing_fields

def format_template_content(template_content: str, parameters: dict) -> str:
    """
    Replaces placeholders in the template with values from the parameters dictionary.
    Case-insensitive replacement.
    
    :param template_content: The template content as a string.
    :param parameters: Dictionary of parameters to replace placeholders.
    :return: The filled template content.
    """
    filled_content = template_content
    try:
        for key, value in parameters.items():
            # Replace placeholders case-insensitively
            filled_content = re.sub(f"{{{{{key}}}}}", value, filled_content, flags=re.IGNORECASE)
    except Exception as e:
        raise ValueError(f"Error while filling the template: {e}")
    return filled_content

def sanitize_email_address(email: str) -> str:
    """
    Sanitizes an email address by stripping leading and trailing spaces.
    
    :param email: The email address as a string.
    :return: The sanitized email address.
    """
    return email.strip()

def send_error_email(admin_email: str, error_message: str):
    """
    Placeholder for sending an error email to the admin in case of a critical failure.
    
    :param admin_email: The admin's email address.
    :param error_message: The error message to send.
    """
    # This would use the same email sending logic as the main email sending function
    logging.warning(f"Simulated sending error email to {admin_email} with message: {error_message}")
