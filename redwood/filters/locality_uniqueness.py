#!/usr/bin/env python
#
# Copyright (c) 2013 In-Q-Tel, Inc/Lab41, All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""
Created on 19 October 2013
@author: Lab41
"""


import time
import operator
import os
import numpy as np
import matplotlib.pyplot as plt
from collections import namedtuple, defaultdict
from hashlib import sha1
from scipy.cluster.vq import vq, kmeans, whiten
from redwood.filters.redwood_filter import RedwoodFilter
import calendar
import random
import warnings
from multiprocessing import Pool, Queue, Manager
import Queue
import redwood.helpers.core as core
import redwood.helpers.visual as visual

warnings.filterwarnings('ignore')

#NOTE: the find_anomalies and do_eval functions are outside the class so that we
#can run them in parallel using the apply_async function for thread pools

def find_anomalies(rows, sorted_results, sorted_code_counts):
    """
    Helper function that given a list of results from kmeans will assign
    scores to each file given their distance form their centroid

    :param rows: output rows to append to
    :param sorted_rows: results from kmeans sorted
    :sorted_code_counts: centroids sorted by number of observations
    """
    #definitely want to adjust these distance thresholds
    distance_threshold0 = 1.0
    distance_threshold1 = 1.5
    distance_threshold2 = 5.0

   #print "Code counts: {} smallest: {} ".format(sorted_code_counts, sorted_code_counts[0][0])
    smallest_count = sorted_code_counts[0][1]
    if smallest_count < 3:
        target = sorted_code_counts[0][0]  #smallest cluster
    else:
        target = -1


    for c, d, r in sorted_results:

        #a lone cluster with less than 3 elements element
        if c == target:
            score = .3
        if d > distance_threshold2:
            score = .1
        elif d> distance_threshold1:
            score = .3
        elif d> distance_threshold0:
            score = .4
        else:
            score = 1
        file_metadata_id = r[0]
        rows.put((file_metadata_id, score))


def do_eval(rows, full_path, files, num_clusters, num_features):
    """
    Helper function that analyzes a directory, looking for outliers in clusters based on the input features.
    Currently, only two static features are analyzed, however future versions could allow
    for selectable set of features

    :param rows: output variable to append results to
    :param full_path: the path that is being analyzed
    :param files: meta data for files in the directory
    :param num_clusters: number of clusters to specify for kmeans
    :param num_features: number of features included
    
    :return: nothing... use the rows input as an output to append to
    """
    
    srt = time.time()
    num_obs = len(files)
   
    if(num_obs < num_clusters):
        for f in files:
            rows.put((f[0], .5))
        return
 
    #zero out the array that will contain the inodes
    observations = np.zeros((num_obs, num_features))

    i = 0
    
    for file_metadata_id,mod_date,full_path,file_name,inode,parent_id, in files:
        seconds = calendar.timegm(mod_date.utctimetuple())
        observations[i][0] = inode
        observations[i][1] = seconds
        i += 1
    
    whitened = whiten(observations) 

    #get the centroids
    codebook,_ = kmeans(whitened, num_clusters)
   
    #sometimes if all observations for a given feature are the same
    #the centroids will not be found.  For now, just throw out that data, but
    #figure out a solution later
    if len(codebook) != num_clusters:
        for f in files:
            rows.put((f[0], .5)) 
        return

    code, dist = vq(whitened, codebook)
    d = defaultdict(int)

    #quick way to get count of cluster sizes        
    for c in code:
        d[c] += 1


    #sorted the map codes to get the smallest to largest cluster
    sorted_codes = sorted(d.iteritems(), key = operator.itemgetter(1))
    
    combined = zip(code, dist, files)
    sorted_results =  sorted(combined, key=lambda tup: tup[0])
    
    find_anomalies(rows, sorted_results, sorted_codes) 
    elp = time.time() - srt
    #print "completed pid {} time: {}".format(os.getpid(), elp)
             



class LocalityUniqueness(RedwoodFilter):
    """
    LocalityUniqueness seeks to identify anomalies through clustering of file features in a given directory. The 
    assumption is that files of interest are those that are different than most of their neighbors in a given 
    domain -- this case being the directory.  As a result, this filter is responsible for giving outliers of clusters
    lower reputation scores than those files closer to the centroid
    """

    def __init__(self, cnx=None):
        self.score_table = "lu_scores"
        self.name = "Locality Uniqueness"
        self.cnx = cnx
    def usage(self):
        """
        Prints the usage statements for the discovery functions
        """
        print "[*] evaluate_dir [full_path] [source] [clusters]"
        print "\t|- runs kmeans and shows scatter plot"
        print "\t| [full_path]  - path to analyze"
        print "\t| [source]     - source where the path exists"
        print "\t| [clusters]   - number of clusters to use"

    def update(self, source):
        """
        Applies the Locality Uniqueness filter to the given source, updating existing data
        analyzed from previous sources. Currently the update function uses 3 clusters for clustering
        analysis.  This will be dynamic in future versions. 

        :param source: media source name
        """
        print "[+] Locality Uniqueness Filter running on {}".format(source)
        self.build()
        self.evaluate_source(source, 3)


    def evaluate_source(self, source_name, num_clusters=3):
        """
        Evaluates and scores a given source with a specified number of clusters for kmeans. Currently
        this function uses two set features as inputs (modification time and inode number), however
        futures versions will allow for dynamic feature inputs

        :param source_name: media source name
        :param num_clusters: number of clusters to input into kmeans (Default: 3)
        """

        cursor = self.cnx.cursor()
        src_info = core.get_source_info(self.cnx, source_name)        

        #returns all files sorted by directory for the given source
        query = """
            SELECT file_metadata_id, last_modified, full_path, file_name, filesystem_id, parent_id, hash 
            FROM joined_file_metadata 
            where source_id = {} order by parent_id asc
            """.format(src_info.source_id)
       
        cursor.execute(query)
       
        files = list()

        print "...Beginning clustering analysis"
        pool = Pool(processes=4)              # start 4 worker processes
        manager = Manager()
        rows = manager.Queue()
        is_first = True
        
        parent_id_prev = None
        #should iterate by dir of a given source at this point
        for(file_metadata_id, last_modified, full_path, file_name, filesystem_id, parent_id, hash_val) in cursor:
            
            if is_first is True:
                is_first = False
                parent_id_prev = parent_id

            #if parent_id is diff than previous, we are in new directory, so pack it up for analysis
            if parent_id_prev != parent_id:
                
                parent_id_prev = parent_id
                
                if len(files) > 0:
                    pool.apply_async(do_eval, [rows, full_path, files, num_clusters, 2])
                    files = list()
            
            #make sure to omit directories from the clustering analy               
            if file_name != '/' and hash_val != "":
                files.append((file_metadata_id, last_modified, full_path,file_name, filesystem_id, parent_id))
       
        if len(files) > 0:
            pool.apply_async(do_eval, [rows, full_path, files, num_clusters, 2])

        pool.close()
        pool.join() 
         
        input_rows = []
        count = 0
        while rows.empty() is False:
            curr = rows.get()
            input_rows.append(curr)
            count +=1
            if count % 50000 is 0:
                print "...sending {} results to server".format(len(input_rows))
                cursor.executemany("""REPLACE INTO locality_uniqueness(file_metadata_id, score) values(%s, %s)""", input_rows)
                input_rows = []
                count=0
        print "...sending {} results to server".format(len(input_rows))
        
        cursor.executemany("""REPLACE INTO locality_uniqueness(file_metadata_id, score) values(%s, %s)""", input_rows)
        self.cnx.commit()
        #need to drop the lu_scores and recalculate
        cursor.execute("drop table if exists lu_scores")
        
        query = ("""CREATE TABLE IF NOT EXISTS `lu_scores` (
                `id` bigint(20) unsigned NOT NULL,
                `score` double DEFAULT NULL,
                KEY `fk_unique_file0_id` (`id`),
                CONSTRAINT `fk_unique_file0_id` FOREIGN KEY (`id`) 
                REFERENCES `unique_file` (`id`) ON DELETE NO ACTION ON UPDATE NO ACTION
                        ) ENGINE=InnoDB""")

        cursor.execute(query) 

        print "...updating scores on the server"
        query = """
            INSERT INTO lu_scores
            (SELECT file_metadata.unique_file_id, avg(locality_uniqueness.score) FROM 
            locality_uniqueness LEFT JOIN file_metadata on (locality_uniqueness.file_metadata_id = file_metadata.id) 
            WHERE file_metadata.unique_file_id is not null
            GROUP BY file_metadata.unique_file_id)
            """
                               
        cursor.execute(query)
        self.cnx.commit()
        

    def clean(self):
        """
        Removes all tables associated with this filter
        """

        cursor = self.cnx.cursor()
        cursor.execute("DROP TABLE IF EXISTS lu_scores")
        cursor.execute("DROP TABLE IF EXISTS locality_uniqueness")
        self.cnx.commit()


    def build(self):
        """
        Build all persistent tables associated with this filter
        """
        cursor = self.cnx.cursor()

        query = ("CREATE table IF NOT EXISTS locality_uniqueness ("
                "file_metadata_id BIGINT unsigned unique,"
                "score DOUBLE NOT NULL,"
                "PRIMARY KEY(file_metadata_id),"
                "INDEX lu_score (score ASC),"
                "CONSTRAINT fk_file_metadata FOREIGN KEY (file_metadata_id)"
                "REFERENCES file_metadata (id)"
                "ON DELETE NO ACTION ON UPDATE NO ACTION"
                 ") ENGINE = InnoDB;")
        
        cursor.execute(query)
        
        self.cnx.commit()
    



    ##################################################
    #
    #       DISCOVERY FUNCTIONS
    #
    ##################################################

    def discover_evaluate_dir(self, dir_name, source, num_clusters=3):
        """
        Discovery function that applies kmeans clustering to a specified directory, displays
        the resulting scatter plot with the clusters, and then prints out an ordered list of 
        the file by the distance from their respective centroid. Currently,
        this function uses two static features of "modification date" and "inode number" but
        future versions will allow for dynamic features inputs.

        :param dir_name: directory name to be analyzed (Required)
        :source: source name to be analzyed (Required)
        :num_clusters: specified number of clusters to use for kmeans (Default: 3)
        """

        num_features = 2
        num_clusters = int(num_clusters)
        cursor = self.cnx.cursor()
        
        if(dir_name.endswith('/')):
            dir_name = dir_name[:-1]

        print "...Running discovery function on source {} at directory {}".format(source, dir_name)
        
        src_info = core.get_source_info(self.cnx, source)
        if src_info is None:
            return [None, None]

        #grab all files for a particular directory from a specific source
        hash_val = sha1(dir_name).hexdigest()
        
        query = ("""SELECT file_name, file_metadata_id, filesystem_id, last_modified
        FROM joined_file_metadata
        WHERE source_id ='{}' AND path_hash = '{}' AND file_name !='/'""").format(src_info.source_id, hash_val)

        cursor.execute(query)

        #bring all results into memory
        sql_results = cursor.fetchall()
      
        if(len(sql_results) == 0):
            return [None, None]

        #zero out the array that will contain the inodes
        filesystem_id_arr = np.zeros((len(sql_results), num_features))

        i = 0
        for _, _,inode, mod_date in sql_results:
            seconds = calendar.timegm(mod_date.utctimetuple())
            filesystem_id_arr[i] = (inode, seconds)
            i += 1
        whitened = whiten(filesystem_id_arr) 
        #get the centroids
        codebook,_ = kmeans(whitened, num_clusters)
        code, dist = vq(whitened, codebook)
        d = defaultdict(int)

        #quick way to get count of cluster sizes        
        for c in code:
            d[c] += 1
      
        #sorted the map codes to get the smallest to largest cluster
        sorted_codes = sorted(d.iteritems(), key = operator.itemgetter(1))
        #sorts the codes and sql_results together as pairs
        combined = zip(dist, code, sql_results)
        sorted_results =  sorted(combined, key=lambda tup: tup[0])

        for dist_val, c, r in sorted_results:
            print "Dist: {} Cluster: {}  Data: {}".format(dist_val,c,r)

        visual.visualize_scatter(d, code, whitened, codebook, 3, "inode number", "modification datetime", dir_name)

