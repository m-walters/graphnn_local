from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from graph_nets import blocks
from graph_nets import graphs
from graph_nets import modules
from graph_nets import utils_np
from graph_nets import utils_tf

import my_graph_tools as mgt
import matplotlib.pyplot as plt
import networkx as nx
import numpy as np
import sonnet as snt
import tensorflow as tf
import h5py
from progressbar import progressbar
from sklearn.preprocessing import normalize
import matplotlib.pyplot as plt

pi = np.pi
twopi = np.pi*2


# Defaults for below were 2 and 16
NUM_LAYERS = 2  # Hard-code number of layers in the edge/node/global models.
LATENT_SIZE = 16  # Hard-code latent layer sizes for demos.
NTG = 144

def make_mlp_model(Lsize=LATENT_SIZE,Nlayer=NUM_LAYERS):
  """Instantiates a new MLP, followed by LayerNorm.

  The parameters of each new MLP are not shared with others generated by
  this function.

  Returns:
    A Sonnet module which contains the MLP and LayerNorm.
  """
  """
  # Version with regularization
  return snt.Sequential([
      snt.nets.MLP([Lsize] * Nlayer, activate_final=True, use_dropout=True,
                  regularizers={"w":tf.keras.regularizers.l2(l=0.01),
                                "b":tf.keras.regularizers.l2(l=0.01)
                        }
                  ),
      snt.LayerNorm()
  ])
  """
  return snt.Sequential([
      snt.nets.MLP([Lsize] * Nlayer, activate_final=True)
  ])


class MLPGraphIndependent(snt.AbstractModule):
  """GraphIndependent with MLP edge, node, and global models."""

  def __init__(self, name="MLPGraphIndependent"):
    super(MLPGraphIndependent, self).__init__(name=name)
    with self._enter_variable_scope():
      self._network = modules.GraphIndependent(
          edge_model_fn=make_mlp_model,
          node_model_fn=make_mlp_model,
          global_model_fn=make_mlp_model
          )

  def _build(self, inputs):
    return self._network(inputs)


class MLPGraphNetwork(snt.AbstractModule):
  """GraphNetwork with MLP edge, node, and global models."""

  def __init__(self, name="MLPGraphNetwork"):
    super(MLPGraphNetwork, self).__init__(name=name)
    with self._enter_variable_scope():
        self._network = \
            modules.GraphNetwork(make_mlp_model, make_mlp_model,
                make_mlp_model,
                global_block_opt={"use_edges":False,"use_nodes":False})
                #lambda:timecrement(NTG,disable=True),

  def _build(self, inputs):
    return self._network(inputs)


def get_empty_graph(nodeshape,edgeshape,glblshape,senders,receivers):
    dic = {
        "globals": np.zeros(glblshape,dtype=np.float),
        "nodes": np.zeros(nodeshape,dtype=np.float),
        "edges": np.zeros(edgeshape,dtype=np.float),
        "senders": senders,
        "receivers": receivers
    }
    return utils_tf.data_dicts_to_graphs_tuple([dic])


class GeoMLP(snt.AbstractModule):
    """For extracting the geographic dependencies of each location"""
    def __init__(self,init_graph,name="GeoMLP"):
        super(GeoMLP, self).__init__(name=name)
        nnode, nedge = init_graph.nodes.shape[0], init_graph.edges.shape[0]
        nnode_ft, nedge_ft, nglbl_ft = 5,7,9
        
        with self._enter_variable_scope():
            self.node_mlp = snt.nets.MLP([nnode_ft])
            self.edge_mlp = snt.nets.MLP([nedge_ft])
            self.glbl_mlp = snt.nets.MLP([nglbl_ft])
            self.geograph = get_empty_graph((nnode,nnode_ft),
                                            (nedge,nedge_ft),
                                            (1,nglbl_ft),
                                            init_graph.senders,
                                            init_graph.receivers
                                           )
    
    def _build(self, inputs):
        self.geograph = self.geograph.replace(
                    nodes=self.node_mlp(inputs.nodes),
                    edges=self.edge_mlp(inputs.edges),
                    globals=self.glbl_mlp(inputs.globals))
        return self.geograph



class EncodeProcessDecode(snt.AbstractModule):
    """Full encode-process-decode model.

    The model we explore includes three components:
    - An "Encoder" graph net, which independently encodes the edge, node, and
      global attributes (does not compute relations etc.). Uses an MLP to expand.
    - A "Core" graph net, which performs N rounds of processing (message-passing)
      steps. The input to the Core is the concatenation of the Encoder's output
      and the previous output of the Core (labeled "Hidden(t)" below, where "t" is
      the processing step).
    - A "Decoder" graph net, which independently decodes the edge, node, and
      global attributes (does not compute relations etc.), on each message-passing
      step.

                        Hidden(t)   Hidden(t+1)
                           |            ^
              *---------*  |  *------*  |  *---------*
              |         |  |  |      |  |  |         |
    Input --->| Encoder |  *->| Core |--*->| Decoder |---> Output(t)
              |         |---->|      |     |         |
              *---------*     *------*     *---------*
    """

    def __init__(self,
                 edge_output_size=None,
                 node_output_size=None,
                 global_output_size=None,
                 name="EncodeProcessDecode"):
        super(EncodeProcessDecode, self).__init__(name=name)
        self._encoder = MLPGraphIndependent()
        self._core = MLPGraphNetwork()
        self._decoder = MLPGraphIndependent()
        # Transforms the outputs into the appropriate shapes.
        if edge_output_size is None:
            edge_fn = None
        else:
            edge_fn = lambda: snt.Linear(edge_output_size, name="edge_output")
        if node_output_size is None:
            node_fn = None
        else:
            node_fn = lambda: snt.Linear(node_output_size, name="node_output")
        with self._enter_variable_scope():
            self._output_transform = \
                modules.GraphIndependent(edge_fn, node_fn)

    def _build(self, input_op, num_processing_steps):
        latent = self._encoder(input_op)
        latent0 = latent
        output_ops = []
        for _ in range(num_processing_steps):
            core_input = utils_tf.concat([latent0, latent], axis=1)
            latent = self._core(core_input)
            decoded_op = self._decoder(latent)
            output_ops.append(self._output_transform(decoded_op).replace(
                              globals=input_op.globals))
        return output_ops



class timecrement(snt.Module):
    # Custom sonnet module for incrementing the global feature. Yeesh
    def __init__(self,ntg,disable=False,name=None):
        self.adder = tf.constant([0.,1.],dtype=np.double)
        self.add_day = tf.Variable([[1.,0.]],dtype=np.double,trainable=False)
        self.add_tg = tf.Variable([[0.,1.]],dtype=np.double,trainable=False)
        self.T = tf.Variable([[0.,0.]],dtype=np.double,trainable=False)
        self.ntg = ntg
        self.disable = disable
    def __call__(self,T):
        if self.disable:
            return T[:,:2]
        day = T[0,0]
        tg = T[0,1]
        self.T = tf.mod(tf.add(T,self.add_tg),tf.constant([[8.,self.ntg]],dtype=np.double))
        def f1(): return tf.mod(tf.add(self.T,self.add_day),\
                                tf.constant([[7.,(self.ntg+2)]],dtype=np.double))
        def f2(): return self.T
        self.T = tf.cond(tf.math.equal(self.T[0,1],0.),f1,f2)
        return self.T


def get_node_coord_dict(h5):
    node_np = h5['node_coords']
    d = {}
    for i,coords in enumerate(node_np):
        d.update({i:(coords[0],coords[1])})
    return d

def draw_graph(graph, node_pos_dict, col_lims=None, is_normed=False, normfile=None):
    if col_lims:
        vmin,vmax = col_lims[0], col_lims[1]
        e_vmin,e_vmax = col_lims[2], col_lims[3]
    else:
        vmin,vmax = -0.5, 10
        e_vmin,e_vmax = -0.5, 5

    if is_normed:
        # Need to unnorm for plotting
        hf = h5py.File(normfile,'r')
        edgestats = hf['edge_stats']
        nodestats = hf['node_stats']
        graph = unnorm_graph(graph,nodestats,edgestats)
        hf.close()


    graphs_nx = utils_np.graphs_tuple_to_networkxs(graph)

    nodecols = graph.nodes[:,0]
    edges = graph.edges
    edgecols = np.zeros((len(edges),))
    for i,e in enumerate(graphs_nx[0].edges):
        j = np.argwhere((graph.senders==e[0]) & (graph.receivers==e[1]))
        edgecols[i] = edges[j,0]

    fig,ax = plt.subplots(figsize=(15,15))
    nx.draw(graphs_nx[0],ax=ax,pos=node_pos_dict,node_color=nodecols,
            edge_color=edgecols,node_size=100,
            cmap=plt.cm.winter,edge_cmap=plt.cm.winter,
            vmin=vmin,vmax=vmax,edge_vmin=e_vmin,edge_vmax=e_vmax,
            arrowsize=10)
    return fig,ax

def snap2graph(h5file,day,tg,use_tf=False,placeholder=False,name=None,normalize=True):
    snapstr = 'day'+str(day)+'tg'+str(tg)
    if normalize:
        glbls = h5file['nn_glbl_features_normed/'+snapstr]
        edges = h5file['nn_edge_features_normed/'+snapstr]
        nodes = h5file['nn_node_features_normed/'+snapstr]
    else:
        glbls = h5file['glbl_features/'+snapstr]
        edges = h5file['nn_edge_features/'+snapstr]
        nodes = h5file['node_features/'+snapstr]
    senders = h5file['senders']
    receivers = h5file['receivers']
    
    node_arr = nodes[:]
    edge_arr = edges[:]
    glbl_arr = glbls[0]

    graphdat_dict = {
        "globals": glbl_arr.astype(np.float),
        "nodes": node_arr.astype(np.float),
        "edges": edge_arr.astype(np.float),
        "senders": senders[:],
        "receivers": receivers[:],
        "n_node": node_arr.shape[0],
        "n_edge": edge_arr.shape[0]
    }

    if not use_tf:
        graphs_tuple = utils_np.data_dicts_to_graphs_tuple([graphdat_dict])
    else:
        if placeholder:
            name = "placeholders_from_data_dicts" if not name else name
            graphs_tuple = utils_tf.placeholders_from_data_dicts([graphdat_dict], name=name)
        else:
            name = "tuple_from_data_dicts" if not name else name
            graphs_tuple = utils_tf.data_dicts_to_graphs_tuple([graphdat_dict], name=name)
            
    return graphs_tuple

def EdgeNodeCovariance(h5_name):
    h5f = h5py.File(h5_name,'a')
    try:
        covs = h5f['edge_node_covs']
        del covs, h5f['edge_node_covs']
    except:
        pass
    senders = h5f['senders']
    receivers = h5f['receivers']
    nedge = senders.shape[0]
    h5_cov = h5f.create_dataset("edge_node_covs",shape=(nedge,3),dtype=np.double)
    
    # Iterate over senders and edges
    # Note that these arrays have corresponding indices
    # Each edge-node pair will have 7*NTG data points, gather these.
    # We will have an array of shape=(nedge,2,3,2,7*NTG)
    # First 2 is for send/receive nodes, and second 2 is for x,y data
    np_dat = np.zeros(shape=(nedge,7*NTG,2,3),dtype=np.float)

    t = 0
    for day in range(7):
        for tg in progressbar(range(NTG)):
            tg_post = (tg+1)%NTG
            day_post = day
            if tg == (NTG-1):
                day_post = (day+1)%7
            edges = h5f['edge_features/day'+str(day)+'tg'+str(tg)]
            nodes_post = h5f['node_features/day'+str(day_post)+'tg'+str(tg_post)]

            for i in range(nedge):
                s,r = senders[i], receivers[i]
                edge = edges[i]
                x = edge[:3]
                y = nodes_post[r]

                np_dat[i,t] = np.array([x,y])
            t += 1

    for i in range(nedge):
        covs = []
        for j in range(3):
            covs.append(np.cov(np_dat[i,:,:,j],rowvar=False)[0,1])
        h5_cov[i] = covs

    h5f.close()

    
def create_nn_inputset(h5_name):
    h5f = h5py.File(h5_name,'a')

    try:
        covs = h5f['edge_node_covs']
    except:
        print("edge_node_covs DNE, exiting.")
        h5f.close()
        return

    try:
        grp = h5f.create_group("nn_edge_features")
    except:
        print("nn_edge_features group already exists. Overwriting")
        del h5f['nn_edge_features']
        grp = h5f.create_group("nn_edge_features")

    ogshape = h5f['edge_features/day0tg0'].shape
    for d in progressbar(range(7)):
        for tg in range(NTG):
            newfts = np.zeros((ogshape[0],10),dtype=np.float64)
            snapstr = "day"+str(d)+"tg"+str(tg)
            edges = h5f['edge_features/'+snapstr]
            newfts[:,:3] = edges[:]
            newfts[:,3:6] = covs[:]
            newfts[:,6:9] = covs[:]*edges[:]
            newfts[:,9] = edges[:,0]*edges[:,1]

            grp.create_dataset(snapstr,data=newfts)

    print("Creating normalized dataset")
    try:
        normed_edge_group = h5f.create_group("nn_edge_features_normed")
        normed_node_group = h5f.create_group("nn_node_features_normed")
        normed_glbl_group = h5f.create_group("nn_glbl_features_normed")
    except:
        print("Normed features exist. Overwriting")
        del h5f['nn_edge_features_normed'], h5f['nn_node_features_normed'],\
            h5f['nn_glbl_features_normed']
        normed_edge_group = h5f.create_group("nn_edge_features_normed")
        normed_node_group = h5f.create_group("nn_node_features_normed")
        normed_glbl_group = h5f.create_group("nn_glbl_features_normed")

    node_stats = np.zeros((2,3),dtype=np.float64)
    edge_stats = np.zeros((2,10),dtype=np.float64)
    glbl_stats = np.zeros((2,2),dtype=np.float64)
    nodegroup = h5f['node_features']
    edgegroup = h5f['nn_edge_features']
    glblgroup = h5f['glbl_features']

    print("Calculating norm stats")
    k = 1 # data iter
    for key,dset in progressbar(nodegroup.items()):
        for row in dset:
            m_k = node_stats[0,:] + (row - node_stats[0,:])/k
            node_stats[1,:] = node_stats[1,:] + (row - node_stats[0,:])*(row - m_k)
            node_stats[0,:] = m_k
            k+=1
    node_stats[1,:] = np.sqrt(node_stats[1,:]/(k-1))
    
    k = 1
    for key,dset in progressbar(edgegroup.items()):
        for row in dset:
            m_k = edge_stats[0,:] + (row - edge_stats[0,:])/k
            edge_stats[1,:] = edge_stats[1,:] + (row - edge_stats[0,:])*(row - m_k)
            edge_stats[0,:] = m_k
            k+=1
    edge_stats[1,:] = np.sqrt(edge_stats[1,:]/(k-1))

    glbl_stats[:] = [[3.0,2.0], [np.mean(range(NTG)),np.std(range(NTG))]]

    # Now that we have the norm stats, we apply it to the existing datasets
    print("Applying norm to feature sets")
    for d in progressbar(range(7)):
        for tg in range(NTG):
            snapstr="day"+str(d)+"tg"+str(tg)
            nodes = nodegroup[snapstr]
            edges = edgegroup[snapstr]
            glbls = glblgroup[snapstr]
            normed_nodes = mynorm(nodes,node_stats[0,:],node_stats[1,:])
            normed_edges = mynorm(edges,edge_stats[0,:],edge_stats[1,:])
            normed_glbls = mynorm(glbls,glbl_stats[0,:],glbl_stats[1,:])
            normed_node_group.create_dataset(snapstr,data=normed_nodes)
            normed_edge_group.create_dataset(snapstr,data=normed_edges)
            normed_glbl_group.create_dataset(snapstr,data=normed_glbls)

    # Save the stats to hdf5
    h5f.create_dataset('node_stats',data=node_stats)
    h5f.create_dataset('edge_stats',data=edge_stats)
    h5f.create_dataset('glbl_stats',data=glbl_stats)

    h5f.close()


def mynorm(nparr,mus,stds):
    return np.divide(np.subtract(nparr,mus),stds)

def my_unnorm(nparr,norms):
    return np.add(np.multiply(nparr,norms[1,:]),norms[0,:])

def unnorm_graph(graph, node_norms, edge_norms):
    return graph.replace(nodes=my_unnorm(graph.nodes,node_norms),
                         edges=my_unnorm(graph.edges,edge_norms))
                                         
def get_norm_stats(h5_name):
    # Iterate over nodes and edges of h5f file to
    # Get the mus, sigmas of the dataset
    h5f = h5py.File(h5_name,'r')
    nodegroup = h5f['nn_node_features']
    edgegroup = h5f['nn_edge_features']
    nedge, nnode = h5f['n_edges'], h5f['n_nodes']
    
    # _stats hold the mu and sigma for each feature
    nodeshape = nodegroup['day0tg0'].shape
    edgeshape = edgegroup['day0tg0'].shape
    node_stats, edge_stats = np.zeros((2,nodeshape[1]),dtype=np.double),\
                             np.zeros((2,edgeshape[1]),dtype=np.double)
    k = 1 # data iter
    for key,dset in progressbar(nodegroup.items()):
        for row in dset:
            m_k = node_stats[0,:] + (row - node_stats[0,:])/k
            node_stats[1,:] = node_stats[1,:] + (row - node_stats[0,:])*(row - m_k)
            node_stats[0,:] = m_k
            k+=1
    node_stats[1,:] = np.sqrt(node_stats[1,:]/(k-1))
    
    k = 1
    for key,dset in progressbar(edgegroup.items()):
        for row in dset:
            m_k = edge_stats[0,:] + (row - edge_stats[0,:])/k
            edge_stats[1,:] = edge_stats[1,:] + (row - edge_stats[0,:])*(row - m_k)
            edge_stats[0,:] = m_k
            k+=1
    edge_stats[1,:] = np.sqrt(edge_stats[1,:]/(k-1))
    h5f.close()
    
    return node_stats, edge_stats
    
def copy_graph(graphs_tuple):
    return utils_np.data_dicts_to_graphs_tuple(
        utils_np.graphs_tuple_to_data_dicts(graphs_tuple))


def get_daytimes():
    daytimes = np.zeros((7*NTG,2),dtype=int)
    i=0
    for d in range(7):
        for tg in range(NTG):
            daytimes[i] = [d,tg]
            i+=1
    return daytimes
    

def get_norm_stats_2(hfname):
    h5f = h5py.File(hfname,'a')
    node_stats = np.zeros((2,3),dtype=np.float64)
    edge_stats = np.zeros((2,10),dtype=np.float64)
    glbl_stats = np.zeros((2,2),dtype=np.float64)
    nodegroup = h5f['node_features']
    edgegroup = h5f['nn_edge_features']
    glblgroup = h5f['glbl_features']

    print("Calculating norm stats")
    k = 1 # data iter
    for key,dset in progressbar(nodegroup.items()):
        for row in dset:
            m_k = node_stats[0,:] + (row - node_stats[0,:])/k
            node_stats[1,:] = node_stats[1,:] + (row - node_stats[0,:])*(row - m_k)
            node_stats[0,:] = m_k
            k+=1
    node_stats[1,:] = np.sqrt(node_stats[1,:]/(k-1))
    
    k = 1
    for key,dset in progressbar(edgegroup.items()):
        for row in dset:
            m_k = edge_stats[0,:] + (row - edge_stats[0,:])/k
            edge_stats[1,:] = edge_stats[1,:] + (row - edge_stats[0,:])*(row - m_k)
            edge_stats[0,:] = m_k
            k+=1
    edge_stats[1,:] = np.sqrt(edge_stats[1,:]/(k-1))

    glbl_stats[:] = [[3.0,2.0], [np.mean(range(NTG)),np.std(range(NTG))]]

    # Save the stats to hdf5
    try:
        del h5f['node_stats'],h5f['edge_stats'],h5f['glbl_stats']
    except:
        pass
    h5f.create_dataset('node_stats',data=node_stats)
    h5f.create_dataset('edge_stats',data=edge_stats)
    h5f.create_dataset('glbl_stats',data=glbl_stats)

    h5f.close()






