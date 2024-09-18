import unittest
from unittest.mock import patch, MagicMock, mock_open
import json
from email_merge_tool import mail_merge, format_template_content, load_email_template, load_recipient_data, create_email_connection

class TestMailMerge(unittest.TestCase):

    def setUp(self):
        # Define a sample email template
        self.sample_template = "Hello {name}, welcome to {company}."
        
        # Define a sample recipient data as dictionary
        self.sample_recipients = [
            {"email": "john.doe@example.com", "name": "John", "company": "Acme Corp"},
            {"email": "jane.smith@example.com", "name": "Jane", "company": "Tech Solutions"}
        ]
        
        # Define a sample recipient CSV content
        self.sample_csv = "email,name,company\njohn.doe@example.com,John,Acme Corp\njane.smith@example.com,Jane,Tech Solutions"
        
        # Define a sample recipient JSON content
        self.sample_json = json.dumps(self.sample_recipients)

    def test_template_loading(self):
        # Test loading an email template from a file
        with patch("builtins.open", mock_open(read_data=self.sample_template)) as mock_file:
            template = load_email_template("email_template.txt")
            mock_file.assert_called_once_with("email_template.txt", 'r', encoding='utf-8')
            self.assertEqual(template, self.sample_template)

    def test_template_filling(self):
        # Test filling the template with recipient data
        recipient_data = {"name": "John", "company": "Acme Corp"}
        filled_template = format_template_content(self.sample_template, recipient_data)
        self.assertEqual(filled_template, "Hello John, welcome to Acme Corp.")

    def test_template_filling_missing_fields(self):
        # Test missing fields handling in template
        recipient_data = {"name": "John"}
        with self.assertRaises(ValueError):
            format_template_content(self.sample_template, recipient_data)

    def test_load_recipient_data_csv(self):
        # Test loading recipient data from CSV
        with patch("builtins.open", mock_open(read_data=self.sample_csv)) as mock_file:
            recipients = load_recipient_data("recipients.csv")
            mock_file.assert_called_once_with("recipients.csv", 'r', newline='', encoding='utf-8')
            self.assertEqual(recipients, self.sample_recipients)

    def test_load_recipient_data_json(self):
        # Test loading recipient data from JSON
        with patch("builtins.open", mock_open(read_data=self.sample_json)) as mock_file:
            recipients = load_recipient_data("recipients.json")
            mock_file.assert_called_once_with("recipients.json", 'r', encoding='utf-8')
            self.assertEqual(recipients, self.sample_recipients)

    @patch("smtplib.SMTP_SSL")
    def test_create_email_connection(self, mock_smtp):
        # Mock a successful SMTP connection
        credentials = {"email": "your-email@example.com", "password": "your-password"}
        mock_smtp.return_value = MagicMock()

        # Create the connection and check that the connection was established correctly
        connection = create_email_connection("gmail", credentials)
        mock_smtp.assert_called_once_with("smtp.gmail.com", 465)
        connection.login.assert_called_once_with("your-email@example.com", "your-password")

    @patch("smtplib.SMTP_SSL")
    @patch("email_merge_tool.send_email")
    def test_mail_merge_success(self, mock_send_email, mock_smtp):
        # Mock sending email and SMTP connection
        credentials = {"email": "your-email@example.com", "password": "your-password"}
        mock_smtp.return_value = MagicMock()
        mock_send_email.return_value = "success"

        # Test the mail_merge function
        with patch("builtins.open", mock_open(read_data=self.sample_template)) as mock_template:
            with patch("builtins.open", mock_open(read_data=self.sample_csv)) as mock_csv:
                mail_merge(
                    template_path="email_template.txt",
                    data_source="recipients.csv",
                    service="gmail",
                    credentials=credentials,
                    is_html=False
                )

                # Assert the template file and recipient file were opened
                mock_template.assert_called_once_with("email_template.txt", 'r', encoding='utf-8')
                mock_csv.assert_called_once_with("recipients.csv", 'r', newline='', encoding='utf-8')

                # Check if emails were sent for both recipients
                self.assertEqual(mock_send_email.call_count, 2)
                mock_send_email.assert_any_call(mock_smtp.return_value, self.sample_recipients[0], "Hello John, welcome to Acme Corp.", False)
                mock_send_email.assert_any_call(mock_smtp.return_value, self.sample_recipients[1], "Hello Jane, welcome to Tech Solutions.", False)

if __name__ == '__main__':
    unittest.main()
