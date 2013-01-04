import os
import math
import copy
import numpy
import subprocess
from lazyflow.rtype import Roi, SubRegion
from lazyflow.graph import Operator, InputSlot, OutputSlot, OrderedSignal
import itertools
import h5py
import time
import warnings
import collections
import tempfile
import shutil
import hashlib
from ilastik.clusterConfig import parseClusterConfigFile
from ilastik.utility.timer import Timer, timed
from lazyflow.blockwiseFileset import BlockwiseFileset
from lazyflow.roi import getIntersectingBlocks

from lazyflow.operators import OpH5WriterBigDataset, OpSubRegion

from lazyflow.bigRequestStreamer import BigRequestStreamer

import logging
logger = logging.getLogger(__name__)

OUTPUT_FILE_NAME_FORMAT = "{} output {}.h5"

class OpTaskWorker(Operator):
    Input = InputSlot()
    RoiString = InputSlot(stype='string')
    TaskName = InputSlot(stype='string')
    ConfigFilePath = InputSlot(stype='filestring')
    OutputFilesetDescription = InputSlot(stype='filestring')
    
    ReturnCode = OutputSlot()

    def __init__(self, *args, **kwargs):
        super( OpTaskWorker, self ).__init__( *args, **kwargs )
        self.progressSignal = OrderedSignal()

    def setupOutputs(self):
        self.ReturnCode.meta.dtype = bool
        self.ReturnCode.meta.shape = (1,)
    
    def execute(self, slot, subindex, ignored_roi, result):
        configFilePath = self.ConfigFilePath.value
        config = parseClusterConfigFile( configFilePath )
        
        blockwiseFileset = BlockwiseFileset( self.OutputFilesetDescription.value, 'a' )
        
        try:
            roiString = self.RoiString.value
            roi = Roi.loads(roiString)
            logger.info( "Executing for roi: {}".format(roi) )
    
            if config.use_node_local_scratch:
                assert False, "FIXME."
    
            assert (blockwiseFileset.getEntireBlockRoi( roi.start )[1] == roi.stop).all(), "Each task must execute exactly one full block.  ({},{}) is not a valid block roi.".format( roi.start, roi.stop )
            assert self.Input.ready()
    
            # Convert the task subrequest shape dict into a shape for this dataset (and axisordering)
            subrequest_shape = map( lambda tag: config.task_subrequest_shape[tag.key], self.Input.meta.axistags )
    
            with Timer() as computeTimer:
                # Stream the data out to disk.
                streamer = BigRequestStreamer(self.Input, (roi.start, roi.stop), subrequest_shape )
                streamer.progressSignal.subscribe( self.progressSignal )
                streamer.resultSignal.subscribe( blockwiseFileset.writeData )
                streamer.execute()
    
                # Now the block is ready.  Update the status.
                blockwiseFileset.setBlockStatus( roi.start, BlockwiseFileset.BLOCK_AVAILABLE )
    
            logger.info( "Finished task in {} seconds".format( computeTimer.seconds() ) )
            result[0] = True
            return result

        finally:
            blockwiseFileset.close()

    def propagateDirty(self, slot, subindex, roi):
        self.ReturnCode.setDirty( slice(None) )

class OpClusterize(Operator):
    Input = InputSlot()
    OutputDatasetDescription = InputSlot()
    ProjectFilePath = InputSlot(stype='filestring')
    ConfigFilePath = InputSlot(stype='filestring')
    
    ReturnCode = OutputSlot()

    # Constants
    FINAL_DATASET_NAME = 'cluster_result'

    class TaskInfo():
        taskName = None
        command = None
        outputFilePath = None
        subregion = None
        
    def setupOutputs(self):
        self.ReturnCode.meta.dtype = bool
        self.ReturnCode.meta.shape = (1,)
    
    def _validateConfig(self):
        if not self._config.use_master_local_scratch:
            assert self._config.node_output_compression_cmd is None, "Can't use node dataset compression unless master local scratch is also used."
    
    def execute(self, slot, subindex, roi, result):
        # We use fabric for executing remote tasks
        # Import it here because it isn't required that the nodes can use it.
        import fabric.api as fab

        success = True
        
        dtypeBytes = self._getDtypeBytes()
        totalBytes = dtypeBytes * numpy.prod(self.Input.meta.shape)
        totalMB = totalBytes / (1000*1000)
        logger.info( "Clusterizing computation of {} MB dataset, outputting according to {}".format(totalMB, self.OutputDatasetDescription.value) )

        configFilePath = self.ConfigFilePath.value
        self._config = parseClusterConfigFile( configFilePath )

        self._validateConfig()

        # Create the destination file if necessary
        blockwiseFileset, taskInfos = self._prepareDestination()

        try:
            # Figure out which work doens't need to be recomputed (if any)
            unneeded_rois = []
            for roi in taskInfos.keys():
                if blockwiseFileset.getBlockStatus == BlockwiseFileset.BLOCK_AVAILABLE:
                    unneeded_rois.append( roi )
    
            # Remove any tasks that we don't need to compute (they were finished in a previous run)
            for roi in unneeded_rois:
                logger.info( "No need to run task: {} for roi: {}".format( taskInfos[roi].taskName, roi ) )
                del taskInfos[roi]
    
            @fab.hosts( self._config.task_launch_server )
            def remoteCommand( cmd ):
                with fab.cd( self._config.server_working_directory ):
                    fab.run( cmd )
    
            # Spawn each task
            for taskInfo in taskInfos.values():
                logger.info("Launching node task: " + taskInfo.command )
                fab.execute( remoteCommand, taskInfo.command )
    
            timeOut = self._config.task_timeout_secs
            serialStepSeconds = 0
            with Timer() as totalTimer:
                # When each task completes, it creates a status file.
                while len(taskInfos) > 0:
                    # TODO: Maybe replace this naive polling system with an asynchronous 
                    #         file status via select.epoll or something like that.
                    if totalTimer.seconds() >= timeOut:
                        logger.error("Timing out after {} seconds, even though {} tasks haven't finished yet.".format( totalTimer.seconds(), len(taskInfos) ) )
                        success = False
                        break
                    time.sleep(15.0)
        
                    logger.debug("Time: {} seconds. Checking {} remaining tasks....".format(totalTimer.seconds(), len(taskInfos)))
        
                    # Locate finished blocks
                    finished_rois = self._determineCompletedBlocks( blockwiseFileset, taskInfos )
    #                # Figure out which results have finished already and copy their results into the final output file
    #                finished_rois = self._copyFinishedResults( taskInfos )
    #                serialStepSeconds += self._copyFinishedResults.prev_run_timer.seconds()
        
                    # Remove the finished tasks from the list we're polling for
                    for roi in finished_rois:
                        del taskInfos[roi]
                    
                    # Handle failured tasks
                    failed_rois = self._checkForFailures( taskInfos )
                    if len(failed_rois) > 0:
                        success = False
        
                    # Remove the failed tasks from the list we're polling for
                    for roi in failed_rois:
                        logger.error( "Giving up on failed task: {} for roi: {}".format( taskInfos[roi].taskName, roi ) )
                        del taskInfos[roi]
    
            if success:
                logger.info( "SUCCESS: Completed {} MB in {} total seconds.".format( totalMB, totalTimer.seconds() ) )
                logger.info( "Reassembly took a total of {} seconds".format( serialStepSeconds ) )
            else:
                logger.info( "FAILED: After {} seconds.".format( totalTimer.seconds() ) )
    
            result[0] = success
            return result
        finally:
            blockwiseFileset.close()
    
    def _getRoiList(self):
        inputShape = self.Input.meta.shape
        blockShape = self._getBlockShape()
        
        rois = []
        for indices in itertools.product( *[ range(0, stop, step) for stop,step in zip(inputShape, blockShape) ] ):
            start=numpy.asarray(indices)
            stop=numpy.minimum( start+blockShape, inputShape )
            rois.append( (start, stop) )

        return rois

    def _getBlockShape(self):
        # Use a dumb means of computing task shapes for now.
        # Find the dimension of the data in xyz, and block it up that way.
        taggedShape = self.Input.meta.getTaggedShape()

        spaceDims = filter( lambda (key, dim): key in 'xyz' and dim > 1, taggedShape.items() ) 
        numJobs = self._config.num_jobs
        numJobsPerSpaceDim = math.pow(numJobs, 1.0/len(spaceDims))
        numJobsPerSpaceDim = int(round(numJobsPerSpaceDim))

        roiShape = []
        for key, dim in taggedShape.items():
            if key in [key for key, value in spaceDims]:
                roiShape.append(dim / numJobsPerSpaceDim)
            else:
                roiShape.append(dim)

        roiShape = numpy.array(roiShape)
        return roiShape

    def _prepareTaskInfos(self, roiList):
        # Divide up the workload into large pieces
        logger.info( "Dividing into {} node jobs.".format( len(roiList) ) )

        taskInfos = collections.OrderedDict()
        for roiIndex, roi in enumerate(roiList):
            roi = ( tuple(roi[0]), tuple(roi[1]) )
            taskInfo = OpClusterize.TaskInfo()
            taskInfo.subregion = SubRegion( None, start=roi[0], stop=roi[1] )
            
            taskName = "JOB{:02}".format(roiIndex)
            outputFileName = OUTPUT_FILE_NAME_FORMAT.format( taskName, str(roi) )
            outputFilePath = os.path.join( self._config.scratch_directory, outputFileName )

            commandArgs = []
            commandArgs.append( "--option_config_file=" + self.ConfigFilePath.value )
            commandArgs.append( "--project=" + self.ProjectFilePath.value )
            commandArgs.append( "--_node_work_=\"" + Roi.dumps( taskInfo.subregion ) + "\"" )
            commandArgs.append( "--process_name={}".format(taskName)  )
            commandArgs.append( "--output_description_file={}".format( self.OutputDatasetDescription.value )  )

            # Check the command format string: We need to know where to put our args...
            commandFormat = self._config.command_format
            assert commandFormat.find("{task_args}") != -1

            taskOutputLogFilename = taskName + ".log"
            taskOutputLogPath = os.path.join( self._config.output_log_directory, taskOutputLogFilename )
            
            allArgs = " " + " ".join(commandArgs) + " "
            taskInfo.taskName = taskName
            taskInfo.command = commandFormat.format( task_args=allArgs, task_name=taskName, task_output_file=taskOutputLogPath )
            taskInfo.outputFilePath = outputFilePath
            taskInfos[roi] = taskInfo

        return taskInfos

    def _prepareDestination(self):
        """
        - If the result file doesn't exist yet, create it (and the dataset)
        - If the result file already exists, return a list of the rois that 
        are NOT needed (their data already exists in the final output)
        """
        originalDescription = BlockwiseFileset.readDescription(self.OutputDatasetDescription.value)
        datasetDescription = copy.copy(originalDescription)

        # Modify description fields as needed
        datasetDescription.axes = reduce(lambda axes,t: axes + t.key, self.Input.meta.axistags, "")
        datasetDescription.shape = list(self.Input.meta.shape)
        if datasetDescription.dtype != self.Input.meta.dtype:
            dtype = self.Input.meta.dtype
            if type(dtype) is numpy.dtype:
                dtype = dtype.type
            datasetDescription.dtype = dtype().__class__.__name__

        assert originalDescription.block_shape is not None

        # Create a unique hash for this blocking scheme.
        # If it changes, we can't use any previous data.
        sha = hashlib.sha1()
        sha.update( str( tuple( datasetDescription.block_shape) ) )
        sha.update( datasetDescription.axes )
        sha.update( datasetDescription.block_file_name_format )

        datasetDescription.hash_id = sha.hexdigest()

        if datasetDescription != originalDescription:
            BlockwiseFileset.writeDescription(self.OutputDatasetDescription.value, datasetDescription)

        # Now open the dataset
        blockwiseFileset = BlockwiseFileset( self.OutputDatasetDescription.value )
        
        taskInfos = self._prepareTaskInfos( blockwiseFileset.getAllBlockRois() )
        
        if blockwiseFileset.description.hash_id != originalDescription.hash_id:
            # Something about our blocking scheme changed.
            # Make sure all blocks are marked as NOT available.
            # (Just in case some were left over from a previous run.)
            for roi in taskInfos.keys():
                blockwiseFileset.setBlockStatus( roi[0], BlockwiseFileset.BLOCK_NOT_AVAILABLE )

        return blockwiseFileset, taskInfos

    def _determineCompletedBlocks(self, blockwiseFileset, taskInfos):
        finished_rois = []
        for roi in taskInfos.keys():
            if blockwiseFileset.getBlockStatus(roi[0]) == BlockwiseFileset.BLOCK_AVAILABLE:
                finished_rois.append( roi )
        return finished_rois

    @timed
    def _copyFinishedResults(self, taskInfos):
        """
        For each of the taskInfos provided:
        - Check to see if we have a status file for that task
        - If so, copy the the data from the task output file into the final output file
        - Remove the task from final dataset's list of 'missing rois'
        
        Return the list of rois that we processed.
        """
        finished_rois = []
        destinationFile = None
        resultDataset = None
        missingRois = None
        for roi, taskInfo in taskInfos.items():
            # Has the task completed yet?
            #logger.debug( "Checking for file: {}".format( taskInfo.statusFilePath ) )
            if not os.path.exists( taskInfo.statusFilePath ):
                continue

            logger.info( "Found status file: {}".format( taskInfo.statusFilePath ) )
#            if not os.path.exists( taskInfo.outputFilePath ):
#                raise RuntimeError( "Error: Could not locate output file from spawned task: " + taskInfo.outputFilePath )

            # Open the destination file if necessary
            if destinationFile is None:
                destinationFile = h5py.File( self.OutputFilePath.value )
                resultDataset = destinationFile[OpClusterize.FINAL_DATASET_NAME]
                assert 'missingRois' in resultDataset.attrs
                missingRois = set( resultDataset.attrs['missingRois'] )

            roiString = Roi.dumps( taskInfo.subregion )
            assert roiString in missingRois, "This task didn't need to be executed: it wasn't missing from the result!"
            
            nodeOutputFilePath = taskInfo.outputFilePath

            # Optionally copy to local tmpdir before copying the node dataset.
            if self._config.use_master_local_scratch:
                with Timer() as fileCopyTimer:
                    # Copy the scratch file to local scratch area before we open it with h5py
                    tempDir = tempfile.mkdtemp()
                    roiString = Roi.dumps( taskInfo.subregion )
                    tmpOutputFilePath = os.path.join(tempDir, roiString + ".h5")

                    # Optionally decompress as we copy    
                    if self._config.node_output_compression_cmd is not None:
                        decompress_cmd = self._config.node_output_decompression_cmd.format( uncompressed_file=tmpOutputFilePath, compressed_file=taskInfo.outputFilePath )
                        logger.info( "Decompressing with command: " + decompress_cmd )
                        retcode = subprocess.call( decompress_cmd, shell=True )
                        if retcode == 0:
                            logger.info( "Finished decompressing after {} seconds".format( fileCopyTimer.seconds() ) )
                        else:
                            logger.error( "Decompression command returned non-zero code: {}".format(retcode) )                    
                    else:
                        logger.info( "Copying {} to {}...".format(taskInfo.outputFilePath, tmpOutputFilePath) )
                        shutil.copyfile(taskInfo.outputFilePath, tmpOutputFilePath)
                        logger.info( "Finished copying after {} seconds".format( fileCopyTimer.seconds() ) )
                nodeOutputFilePath = tmpOutputFilePath

            # Open the file
            with h5py.File( nodeOutputFilePath, 'r' ) as f:
                # Check the result
                assert 'node_result' in f.keys()
                assert numpy.all(f['node_result'].shape == numpy.subtract(roi[1], roi[0]))
                assert f['node_result'].dtype == self.Input.meta.dtype
                assert f['node_result'].attrs['axistags'] == self.Input.meta.axistags.toJSON()
    
                shape = f['node_result'][:].shape

                dtypeBytes = self._getDtypeBytes()
                
                # Copy the data into our result (which might be an h5py dataset...)
                key = taskInfo.subregion.toSlice()
    
                with Timer() as copyTimer:
                    resultDataset[key] = f['node_result'][:]
    
                totalBytes = dtypeBytes * numpy.prod(shape)
                totalMB = totalBytes / (1000*1000)
    
                logger.info( "Copying {} MB hdf5 slice took {} seconds".format(totalMB, copyTimer.seconds() ) )
                finished_rois.append(roi)

                # Remove the roi from the list of remaining rois
                roiString = Roi.dumps(taskInfo.subregion)
                missingRois.remove( roiString )

            if self._config.use_master_local_scratch:
                os.remove(tmpOutputFilePath)

        # For now, we close the file after every pass in case something goes horribly wrong...
        if destinationFile is not None:
            # Update the list of rois that are still missing from the output file.
            resultDataset.attrs['missingRois'] = list(missingRois)
            destinationFile.close()
            destinationFile = None

        return finished_rois

    def _checkForFailures(self, taskInfos):
        return []

    def propagateDirty(self, slot, subindex, roi):
        self.ReturnCode.setDirty( slice(None) )


    def _getDtypeBytes(self):
        """
        Return the size of the dataset dtype in bytes.
        """
        dtype = self.Input.meta.dtype
        if type(dtype) is numpy.dtype:
            # Make sure we're dealing with a type (e.g. numpy.float64),
            #  not a numpy.dtype
            dtype = dtype.type
        
        return dtype().nbytes




























