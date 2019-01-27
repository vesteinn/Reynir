# Greynir - Setup instructions for GNU/Linux (Debian/Ubuntu)

The following instructions assume you are running a reasonably modern 
version of Debian or Ubuntu and have `sudo` access.

## Locale

Set up Icelandic locale `is_IS.utf8`:

##### Debian

```
sudo dpkg-reconfigure locales
```

##### Ubuntu

```
sudo locale-gen is_IS.UTF-8
sudo update-locale
```

NB: If PostgreSQL is already running, it needs to be restarted:

```
sudo systemctl restart postgresql
```

## Set up Python virtualenv

Install Python 3:

```
sudo apt-get install python3
```

Make sure you have the latest version of `pip` and `virtualenv`.

```
sudo -H pip3 install --upgrade pip
pip3 install --upgrade virtualenv
```

Install [git](https://git-scm.com) if it's not already installed:

```
sudo apt-get install git
```

Install PyPy 3.5 or later ([available here](http://pypy.org/download.html)). 
For example:

```
mkdir ~/pypy
cd ~/pypy
wget https://bitbucket.org/pypy/pypy/downloads/pypy3-v6.0.0-linux64.tar.bz2
tar --strip-components=1 -xvf pypy3-v6.0.0-linux64.tar.bz2
```

The PyPy binary should now be installed in `~/pypy/bin/pypy3`.

Check out the Greynir repo:

```
cd ~
git clone https://github.com/vthorsteinsson/Reynir
cd ~/Reynir
```

Create and activate virtual environment, install required Python packages:

```
virtualenv -p ~/pypy/bin/pypy3 venv
source venv/bin/activate
pip3 install -r requirements.txt
```

## Set up database

### Install PostgreSQL

Install PostgreSQL 9.5 or later (Greynir relies on the UPSERT feature 
introduced in version 9.5).

```
sudo apt-get install postgresql-contrib postgresql-client libpq-dev
```

Permit user access to PostgreSQL from localhost by editing `pg_hba.conf`
(replace `9.5` in the path with your version):

```
sudo nano /etc/postgresql/9.5/main/pg_hba.conf
```

Make sure that the config file contains the following entries:

```
# IPv4 local connections:
host    all       all             127.0.0.1/32      trust
# IPv6 local connections:
host    all       all             ::1/128           trust
```

Restart PostgreSQL for the changes to take effect:

```
sudo systemctl reload postgresql
```

### Set up users

Change to the default PostgreSQL user `postgres`:

```
sudo su - postgres
```

Launch PostgreSQL client and create database users 
(replace *your_user_name* with your username):

```
psql
create user reynir with password 'reynir';
create user your_user_name;
alter role your_user_name with superuser;
```

### Create database

```
create database scraper with encoding 'UTF8' LC_COLLATE='is_IS.utf8' LC_CTYPE='is_IS.utf8' TEMPLATE=template0;
```

Enable uuid extension:

```
\c scraper
create extension if not exists "uuid-ossp";
```

Verify that the uuid extension is enabled:

```
select * from pg_extension;
```

and then `\q` to quit the `psql` client.

Finally, create the database tables used by Greynir (this will only create
the tables if needed, and no existing data is erased):

```
cd ~/Reynir
python scraperinit.py
```

## Run

Change to the Reynir repo directory and activate the virtual environment:

```
cd ~/Reynir
source venv/bin/activate
```

You should now be able to run Greynir.

##### Web application

```
python main.py
```

Defaults to running on [`localhost:5000`](http://localhost:5000) but this 
can be changed in `config/Reynir.conf`.

##### Scrapers

```
python scraper.py
```

##### Interactive shell

```
./shell.sh
```

Starts an [IPython](https://ipython.org) shell with a database session (`s`), 
the Reynir parser (`r`) and all SQLAlchemy database models preloaded. For more 
info, see [Using the Greynir Shell](shell.md).
