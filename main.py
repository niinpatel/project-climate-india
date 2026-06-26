import ee
from google.oauth2 import service_account

KEY_PATH = '/Users/nitin/project-climate-india/service-account-key.json'

PROJECT_ID = 'experiments-487610'

credentials = service_account.Credentials.from_service_account_file(KEY_PATH)
scoped_credentials = credentials.with_scopes(['https://www.googleapis.com/auth/cloud-platform'])

# 4. Initialize Earth Engine with the scoped service account credentials
ee.Initialize(credentials=scoped_credentials, project=PROJECT_ID)

# 5. Test the connection by requesting metadata for a digital elevation model
test_image = ee.Image('USGS/SRTMGL1_003')
print("Successfully connected! Image metadata:", test_image.getInfo())