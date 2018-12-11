import awsutils as awsutils
import plugininfo
import datetime
import json 
import subprocess
import urllib2
from logger.logger import get_logger

class MetricDataStatistic(object):
    """
    The MetricDataStatistic object encapsulates the information sent with putMetricData.
    
    Keyword arguments:
    namespace -- the default name space for CloudWatch metric (default defined by NAMESPACE)
    metric_name -- the metric identifier (default '')
    timestamp -- the time stamp in AWS format (default current date-time)
    value -- the raw metric value (default None)
    statistics -- the MetricDataStatistic.Statistics object used to aggregate raw values (default None)
    """
    NAMESPACE = plugininfo.NAMESPACE
    
    def __init__(self, metric_name='', unit="", dimensions={}, statistic_values=None,
                 timestamp=None, namespace=NAMESPACE):
        """ Constructor """
        self.namespace = namespace
        self.metric_name = metric_name
        self.unit = unit
        self.dimensions = dimensions
        self.statistics = statistic_values
        if timestamp:
            self.timestamp = timestamp
        else:
            self.timestamp = awsutils.get_aws_timestamp() 
        
    def add_value(self, value):
        if not self.statistics:
            self.statistics = self.Statistics(value) 
        else:
            self.statistics._add_value(value)
        
    class Statistics:
        """
        The Statistics object encapsulates the aggregated metric values used by MetricDataStatistic.
        
        Keyword arguments:
        min -- the minimum aggregated value (default None)
        max -- the maximum aggregated value (default None)
        sum -- the sum of all aggregated values (default None)
        avg -- the average of all aggregated values (default None)
        sample_count -- the count of aggregated values (default 0)
        """
        
        def __init__(self, value):
            """ Constructor """
            self.min = value
            self.max = value
            self.sum = value
            self.sample_count = 1
        
        def _add_value(self, value):
            """ 
            Add new value and recalculate the statistics
            
            Keyword arguments:
            value -- new value to be included in the statistics
            """
            if value > self.max:
                self.max = value
            if value < self.min:
                self.min = value
            self.sum += value
            self.sample_count += 1


class MetricDataBuilder(object):
    """
    The metric data builder is responsible for translating Collectd value list objects
    to CloudWatch MetricData.
    
    Keyword arguments:
    config_helper -- The ConfigHelper object with configuration loaded
    vl -- The Collectd ValueList object with metric information
    adjusted_time - The adjusted_time is the time adjusted according to storage resolution
    """
    _LOGGER = get_logger(__name__)
    
    def __init__(self, config_helper, vl, adjusted_time=None):
        self.config = config_helper
        self.vl = vl
        self.adjusted_time = adjusted_time

    def build(self):
        """ Builds metric data object with name and dimensions but without value or statistics """
        metric_array = [MetricDataStatistic(metric_name=self._build_metric_name(), dimensions=self._build_metric_dimensions(), timestamp=self._build_timestamp())]
        if self.config.push_asg:
            metric_array.append(MetricDataStatistic(metric_name=self._build_metric_name(), dimensions=self._build_asg_dimension(), timestamp=self._build_timestamp()))
        if self.config.push_constant:
            metric_array.append(MetricDataStatistic(metric_name=self._build_metric_name(), dimensions=self._build_constant_dimension(), timestamp=self._build_timestamp()))
        return metric_array
        
    def _build_timestamp(self):
        return datetime.datetime.utcfromtimestamp(self.adjusted_time).strftime('%Y%m%dT%H%M%SZ') if self.config.enable_high_resolution_metrics else None

    def _build_metric_name(self): 
        """
        Creates single string metric name from the Collectd ValueList naming format by flattening the
        multilevel structure into the string in the following format: "plugin.type.type_instance".
        The not required name parts (type_instance) will be skipped if empty.
        """
        name_builder = [str(self.vl.plugin)]
        name_builder.append(str(self.vl.type))
        if self.vl.type_instance:
            name_builder.append(str(self.vl.type_instance))
        return ".".join(name_builder)
    
    def _build_asg_dimension(self):
        dimensions = {
              "AutoScalingGroup" : self._get_autoscaling_group(),
              "PluginInstance" : self._get_plugin_instance_dimension()
              }
        return dimensions

    def _build_constant_dimension(self):
        dimensions = {
              "FixedDimension" : self.config.constant_dimension_value,
              "PluginInstance" : self._get_plugin_instance_dimension()
              }
        return dimensions

    def _build_metric_dimensions(self):
        dimensions = {}
        metadata = self._get_metadata()
        if len(self.config.dimensions) != 0:
            for dim in self.config.dimensions:
                if dim.lower() == 'host':
                    dimensions["Host"] = self._get_host_dimension()
                elif dim.lower() == 'plugininstance':
                    dimensions[dim] = self._get_plugin_instance_dimension()
                else:
                    dimensions[dim] = str(metadata.get(dim[:1].lower() + dim[1:]))
                    self._LOGGER.info("Dimension: " + dim + " Dimensions value: " + str(metadata.get(dim[:1].lower() + dim[1:])))
                    self._LOGGER.info("Length: " + len(self.config.dimensions))  
                    self._LOGGER.info("Type: " + type(self.config.dimensions))
            if self.config.push_asg:
                dimensions["AutoScalingGroup"] = self._get_autoscaling_group()
            if self.config.push_constant:
                dimensions["FixedDimension"] = self.config.constant_dimension_value
        else:
            dimensions = {
                    "Host" : self._get_host_dimension(),
                    "PluginInstance" : self._get_plugin_instance_dimension(),
                    }
            if self.config.push_asg:
                dimensions["AutoScalingGroup"] = self._get_autoscaling_group()
            if self.config.push_constant:
                dimensions["FixedDimension"] = self.config.constant_dimension_value
            self._LOGGER.info("Dimension: " + dimensions)
        return dimensions

    def _get_plugin_instance_dimension(self):
        if self.vl.plugin_instance:
            return self.vl.plugin_instance
        return "NONE"

    def _get_host_dimension(self):
        if self.config.host:
            return self.config.host
        return self.vl.host

    def _get_autoscaling_group(self):
        if self.config.asg_name:
            return self.config.asg_name
        return "NONE"

    def _get_metadata(self):
        ## regarding commandlist below:
            ## http://docs.aws.amazon.com/AWSEC2/latest/UserGuide/instance-identity-documents.html
	        ## Due to the dynamic nature of instance identity documents and signatures,
        ## we recommend retrieving the instance identity document and signature regularly.
        dimensionsList = []
        url = "http://169.254.169.254/latest/dynamic/instance-identity/document"

        req = urllib2.Request(url)
        try:
            response = urllib2.urlopen(req)
        except urllib2.URLError as e:
            if hasattr(e, 'reason'):
                self._LOGGER.error("We failed to reach a server. Reason: " + e.reason)
            elif hasattr(e, 'code'):
                self._LOGGER.error("The server couldn't fulfill the request. Error code: " + e.code)
        for line in response:
            dimensionsList.append(line.rstrip())
        output = "".join(dimensionsList)
        instanceDimensions = json.loads(output)
        return instanceDimensions