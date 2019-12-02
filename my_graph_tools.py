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
NUM_LAYERS = 1  # Hard-code number of layers in the edge/node/global models.
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
          node_model_fn=make_mlp_model)

  def _build(self, inputs):
    return self._network(inputs)


class MLPGraphNetwork(snt.AbstractModule):
  """GraphNetwork with MLP edge, node, and global models."""

  def __init__(self, name="MLPGraphNetwork"):
    super(MLPGraphNetwork, self).__init__(name=name)
    with self._enter_variable_scope():
        self._network = \
            modules.GraphNetwork(make_mlp_model, make_mlp_model,
                lambda:timecrement(NTG,disable=True),
                global_block_opt={"use_edges":False,"use_nodes":False})

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
                 init_graph = None,
                 name="EncodeProcessDecode"):
        super(EncodeProcessDecode, self).__init__(name=name)
        self.init_graph = get_empty_graph(nodeshape=init_graph.nodes.shape,
                                          edgeshape=init_graph.edges.shape,
                                          glblshape=init_graph.globals.shape,
                                          senders=init_graph.senders,
                                          receivers=init_graph.receivers
                                         )
        self._geomlp = GeoMLP(self.init_graph)
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
        geo_out = self._geomlp(input_op)
        print("\nGEO OUT")
        print(geo_out)
        latent = self._encoder(geo_out)
        print("\nLatent0")
        print(latent)
        latent0 = latent
        output_ops = []
        for _ in range(num_processing_steps):
            core_input = utils_tf.concat([latent0, latent], axis=1)
            print("\nCORE INP")
            print(core_input)
            latent = self._core(core_input)
            decoded_op = self._decoder(latent)
            output_ops.append(self._output_transform(decoded_op))
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
            return T
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

def draw_graph(graph, node_pos_dict, col_lims=None):
    if col_lims:
        vmin,vmax = col_lims[0], col_lims[1]
        e_vmin,e_vmax = col_lims[2], col_lims[3]
    else:
        vmin,vmax = -0.5, 10
        e_vmin,e_vmax = -0.5, 5

    nodecols = graph.nodes[:,0]
    edgecols = graph.edges[:,0]

    graphs_nx = utils_np.graphs_tuple_to_networkxs(graph)
    fig,ax = plt.subplots(figsize=(15,15))
    nx.draw(graphs_nx[0],ax=ax,pos=node_pos_dict,node_color=nodecols,
            edge_color=edgecols,node_size=100,
            cmap=plt.cm.winter,edge_cmap=plt.cm.winter,
            vmin=vmin,vmax=vmax,edge_vmin=e_vmin,edge_vmax=e_vmax,
            arrowsize=10)
    return fig,ax

def snap2graph(h5file,day,tg,use_tf=False,placeholder=False,name=None,normalize=True):
    snapstr = 'day'+str(day)+'tg'+str(tg)
    glbls = h5file['glbl_features/'+snapstr][0] # Seems glbls have extra dimension
    nodes = h5file['node_features/'+snapstr]
    edges = h5file['edge_features/'+snapstr]
    senders = h5file['senders']
    receivers = h5file['receivers']
    
    node_arr = nodes[:]
    edge_arr = edges[:]
    glbl_arr = glbls[:]
    
    if normalize:
        node_norms = h5file.attrs['node_norms'][:]
        edge_norms = h5file.attrs['edge_norms'][:]
        node_arr = mynorm(node_arr,node_norms[0,:],node_norms[1,:])
        edge_arr = mynorm(edge_arr,edge_norms[0,:],edge_norms[1,:])
        glbl_arr = np.divide(glbl_arr,[6.,NTG-1])

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
    nodegroup = h5f['node_features']
    edgegroup = h5f['edge_features']
    
    # _stats hold the mu and sigma for each feature
    node_stats, edge_stats = np.zeros((2,3),dtype=np.double),\
                             np.zeros((2,6),dtype=np.double)
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


