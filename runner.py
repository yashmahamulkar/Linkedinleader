from config import ConfigManager
from keymanager import KeyManager
from preferencemanager import PreferenceManager
from emailmanager import TemplateLoader 


config_manager = ConfigManager('./configs/config.json')
keymanager = KeyManager(config_manager.get("scraper_keys_path"), key_type="scraper")
#print(keymanager.get_next_key())
preferences = PreferenceManager("./configs/preferences.json")   
#print(preferences.common_locations())
#print(config_manager.config)

template= TemplateLoader(base_dir="templates")
print(template.get_template('software_dev'))
print(template.get_template('ai'))



