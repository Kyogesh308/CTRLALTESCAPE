import pandas as pd

file_path = 'Participant_details/Event_Registration_23_Plus_40_Dummy.xlsx'
df = pd.read_excel(file_path)

# Look at the first row (the user said "using up the 1st placeholder with a random name")
# The name might be "Chaitrareddy" and Roll Number might be something.
print("Before:")
print(df.head(5))

# We'll replace the first row's Name with '1' and Roll Number with some dummy roll number if needed
# Let's see what the other placeholders look like. 
# Are there any rows where Name is a digit?
placeholders = df[df['Name'].astype(str).str.match(r'^\d+$', na=False)]
if not placeholders.empty:
    print("\nExample placeholders:")
    print(placeholders.head())
    
