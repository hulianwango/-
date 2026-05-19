import bcrypt
import getpass

password = getpass.getpass("OAuth password: ").encode("utf-8")
password_hash = bcrypt.hashpw(password, bcrypt.gensalt()).decode("utf-8")
print(password_hash)
