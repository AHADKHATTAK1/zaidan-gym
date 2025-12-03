from datetime import datetime

# Get admission date from user
admission_date_str = input("Enter admission date (YYYY-MM-DD): ")

# Parse the date string
admission_date = datetime.strptime(admission_date_str, "%Y-%m-%d")

# Display the formatted date
print(f"Admission date: {admission_date.strftime('%Y-%m-%d')}")
