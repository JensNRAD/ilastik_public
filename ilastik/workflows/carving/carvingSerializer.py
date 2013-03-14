from ilastik.applets.base.appletSerializer import AppletSerializer, getOrCreateGroup, deleteIfPresent
import numpy

class CarvingSerializer( AppletSerializer ):
    def __init__(self, carvingTopLevelOperator, *args, **kwargs):
        super(CarvingSerializer, self).__init__(*args, **kwargs)
        self._o = carvingTopLevelOperator 
        
    def _serializeToHdf5(self, topGroup, hdf5File, projectFilePath):
        obj = getOrCreateGroup(topGroup, "objects")
        for imageIndex, opCarving in enumerate( self._o.opCarving.innerOperators ):
            mst = opCarving._mst 
            for name in opCarving._dirtyObjects:
                print "[CarvingSerializer] serializing %s" % name
               
                if name in obj and name in mst.object_seeds_fg_voxels: 
                    #group already exists
                    print "  -> changed"
                elif name not in mst.object_seeds_fg_voxels:
                    print "  -> deleted"
                else:
                    print "  -> added"
                    
                g = getOrCreateGroup(obj, name)
                deleteIfPresent(g, "fg_voxels")
                deleteIfPresent(g, "bg_voxels")
                deleteIfPresent(g, "sv")
                deleteIfPresent(g, "bg_prio")
                deleteIfPresent(g, "no_bias_below")
                
                if not name in mst.object_seeds_fg_voxels:
                    #this object was deleted
                    deleteIfPresent(obj, name)
                    continue
               
                v = mst.object_seeds_fg_voxels[name]
                v = [v[i][:,numpy.newaxis] for i in range(3)]
                v = numpy.concatenate(v, axis=1)
                g.create_dataset("fg_voxels", data=v)
                v = mst.object_seeds_bg_voxels[name]
                v = [v[i][:,numpy.newaxis] for i in range(3)]
                v = numpy.concatenate(v, axis=1)
                g.create_dataset("bg_voxels", data=v)
                g.create_dataset("sv", data=mst.object_lut[name])
                
                d1 = numpy.asarray(mst.bg_priority[name], dtype=numpy.float32)
                d2 = numpy.asarray(mst.no_bias_below[name], dtype=numpy.int32)
                g.create_dataset("bg_prio", data=d1)
                g.create_dataset("no_bias_below", data=d2)
                
            opCarving._dirtyObjects = set()
        
    def _deserializeFromHdf5(self, topGroup, groupVersion, hdf5File, projectFilePath):
        obj = topGroup["objects"]
        
        for imageIndex, opCarving in enumerate( self._o.opCarving.innerOperators ):
            mst = opCarving._mst 
            
            for i, name in enumerate(obj):
                print " loading object with name='%s'" % name
                try:
                    g = obj[name]
                    fg_voxels = g["fg_voxels"]
                    bg_voxels = g["bg_voxels"]
                    fg_voxels = [fg_voxels[:,k] for k in range(3)]
                    bg_voxels = [bg_voxels[:,k] for k in range(3)]
                    
                    sv = g["sv"].value
                  
                    mst.object_names[name]           = i+1 
                    mst.object_seeds_fg_voxels[name] = fg_voxels
                    mst.object_seeds_bg_voxels[name] = bg_voxels
                    mst.object_lut[name]             = sv
                    mst.bg_priority[name]            = g["bg_prio"].value
                    mst.no_bias_below[name]          = g["no_bias_below"].value
                    
                    print "[CarvingSerializer] de-serializing %s, with opCarving=%d, mst=%d" % (name, id(opCarving), id(mst))
                    print "  %d voxels labeled with green seed" % fg_voxels[0].shape[0] 
                    print "  %d voxels labeled with red seed" % bg_voxels[0].shape[0] 
                    print "  object is made up of %d supervoxels" % sv.size
                    print "  bg priority = %f" % mst.bg_priority[name]
                    print "  no bias below = %d" % mst.no_bias_below[name]
                except Exception as e:
                    print 'object %s could not be loaded due to exception: %s'% (name,e)
                
            opCarving._buildDone()
           
    def isDirty(self):
        for index, innerOp in enumerate(self._o.opCarving.innerOperators):
            if len(innerOp._dirtyObjects) > 0:
                return True
        return False
    
    #this is present only for the serializer AppletInterface
    def unload(self):
        pass
    
