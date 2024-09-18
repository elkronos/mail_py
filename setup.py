from setuptools import setup, find_packages

setup(
    name='email_merge_tool',
    version='0.1.0',
    description='A Python package for performing email mail merges with template customization.',
    author='A. Student',
    author_email='email',
    url='https://github.com/yourusername/email_merge_tool',  # Replace with your repository URL
    license='MIT',
    packages=find_packages(),
    install_requires=[
        # Add third-party dependencies here
    ],
    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Developers',
        'Topic :: Software Development :: Libraries :: Python Modules',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
    ],
    python_requires='>=3.6',
    include_package_data=True,
    entry_points={
        'console_scripts': [
            'email-merge=email_merge_tool.merge:mail_merge',  # Allows email-merge command line execution
        ],
    },
)
