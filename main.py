import ee
from google.oauth2 import service_account

KEY_PATH = '/Users/nitin/project-climate-india/service-account-key.json'

PROJECT_ID = 'experiments-487610'

credentials = service_account.Credentials.from_service_account_file(KEY_PATH)
scoped_credentials = credentials.with_scopes(['https://www.googleapis.com/auth/cloud-platform'])

ee.Initialize(credentials=scoped_credentials, project=PROJECT_ID)


# Testing API calls for the final script.
point = ee.Geometry.Point([72.8777, 19.0760]) # Mumbai coordinates
collection = ee.ImageCollection("LANDSAT/LC08/C02/T1_L2")
filtered_collection = collection.filterBounds(point).filterDate('2023-04-01', '2023-04-30')

first_image = filtered_collection.first()
# Select the Surface Temperature band
thermal_band = first_image.select('ST_B10')
print("Band details:", thermal_band.bandNames().getInfo())

lulc_collection = ee.ImageCollection("MODIS/061/MCD12Q1")
# Fetch the map for a complete year
lulc_2022 = lulc_collection.filterDate('2022-01-01', '2022-12-31').first()
print("LULC Bands available:", lulc_2022.bandNames().getInfo())